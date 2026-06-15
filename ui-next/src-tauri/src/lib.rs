use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use sha1::{Digest, Sha1};
use portable_pty::{native_pty_system, CommandBuilder, MasterPty, PtySize};
use std::collections::{HashMap, HashSet};
use std::fs::{self, OpenOptions};
use std::io::{Read, Write};
use std::path::{Path, PathBuf};
use std::process::Command;
use std::sync::{Arc, Mutex};
use tauri::{AppHandle, Emitter, State};
use time::format_description::well_known::Rfc3339;
use time::OffsetDateTime;
use uuid::Uuid;

struct TerminalSession {
    master: Box<dyn MasterPty + Send>,
    writer: Box<dyn Write + Send>,
    child: Box<dyn portable_pty::Child + Send + Sync>,
    buffer: Arc<Mutex<String>>,
}

#[derive(Default)]
struct TerminalManager {
    sessions: Mutex<HashMap<String, TerminalSession>>,
}

#[derive(Clone, Serialize)]
struct TerminalOutput {
    workspace_id: String,
    data: String,
}

fn home_dir() -> PathBuf {
    std::env::var("HOME")
        .map(PathBuf::from)
        .unwrap_or_else(|_| std::env::temp_dir())
}

fn config_dir() -> PathBuf {
    home_dir().join(".config/agent-workbench")
}

fn config_path() -> PathBuf {
    config_dir().join("config.json")
}

fn sessions_dir() -> PathBuf {
    config_dir().join("sessions")
}

fn artifacts_dir() -> PathBuf {
    config_dir().join("artifacts")
}

fn validate_session_id(id: &str) -> Result<(), String> {
    if id.is_empty()
        || !id
            .chars()
            .all(|character| character.is_ascii_alphanumeric() || matches!(character, '-' | ':' | '_'))
    {
        return Err(String::from("invalid session id"));
    }
    Ok(())
}

fn session_path(id: &str) -> Result<PathBuf, String> {
    validate_session_id(id)?;
    Ok(sessions_dir().join(format!("{id}.json")))
}

fn read_json(path: &Path) -> Result<Value, String> {
    let content = fs::read_to_string(path)
        .map_err(|error| format!("could not read {}: {error}", path.display()))?;
    serde_json::from_str(&content)
        .map_err(|error| format!("invalid JSON in {}: {error}", path.display()))
}

fn write_json_atomic(path: &Path, value: &Value) -> Result<(), String> {
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent)
            .map_err(|error| format!("could not create {}: {error}", parent.display()))?;
    }
    let temporary = path.with_extension(format!("tmp-{}-{}", std::process::id(), Uuid::new_v4()));
    let encoded = serde_json::to_vec_pretty(value)
        .map_err(|error| format!("could not encode JSON: {error}"))?;
    let mut file = fs::File::create(&temporary)
        .map_err(|error| format!("could not create {}: {error}", temporary.display()))?;
    file.write_all(&encoded)
        .and_then(|_| file.write_all(b"\n"))
        .and_then(|_| file.sync_all())
        .map_err(|error| format!("could not write {}: {error}", temporary.display()))?;
    fs::rename(&temporary, path)
        .map_err(|error| format!("could not replace {}: {error}", path.display()))
}

fn now_iso() -> String {
    OffsetDateTime::now_utc()
        .format(&Rfc3339)
        .unwrap_or_else(|_| String::new())
}

#[derive(Debug, Deserialize)]
struct SessionSummarySource {
    id: String,
    #[serde(default)]
    title: String,
    #[serde(default)]
    agent: String,
    #[serde(default)]
    backend: Value,
    #[serde(default)]
    context: Value,
    #[serde(default)]
    cwd: String,
    #[serde(default)]
    origin: String,
    #[serde(default)]
    created_at: String,
    #[serde(default)]
    updated_at: String,
    #[serde(default)]
    transcript_loaded: bool,
    #[serde(default)]
    source_session_id: String,
    #[serde(default)]
    source_path: String,
}

#[derive(Debug, Serialize)]
struct SessionSummary {
    id: String,
    title: String,
    agent: String,
    backend: Value,
    context: Value,
    cwd: String,
    origin: String,
    created_at: String,
    updated_at: String,
    transcript_loaded: bool,
    source_session_id: String,
    source_path: String,
}

impl From<SessionSummarySource> for SessionSummary {
    fn from(source: SessionSummarySource) -> Self {
        Self {
            id: source.id,
            title: source.title,
            agent: source.agent,
            backend: source.backend,
            context: source.context,
            cwd: source.cwd,
            origin: source.origin,
            created_at: source.created_at,
            updated_at: source.updated_at,
            transcript_loaded: source.transcript_loaded,
            source_session_id: source.source_session_id,
            source_path: source.source_path,
        }
    }
}

#[derive(Default)]
struct DeletedSessionRefs {
    ids: HashSet<String>,
    paths: HashSet<String>,
}

fn deleted_session_refs() -> DeletedSessionRefs {
    let Ok(config) = read_json(&config_path()) else {
        return DeletedSessionRefs::default();
    };
    let mut deleted = DeletedSessionRefs::default();
    let Some(buckets) = config
        .get("deleted_session_tombstones")
        .and_then(Value::as_object)
    else {
        return deleted;
    };
    for bucket in buckets.values() {
        if let Some(ids) = bucket.get("ids").and_then(Value::as_array) {
            for id in ids {
                if let Some(id) = id.as_str() {
                    deleted.ids.insert(id.to_string());
                }
            }
        }
        if let Some(paths) = bucket.get("paths").and_then(Value::as_array) {
            for path in paths {
                if let Some(path) = path.as_str() {
                    deleted.paths.insert(path.to_string());
                }
            }
        }
    }
    deleted
}

fn backend_session_id(source: &SessionSummarySource) -> String {
    if !source.source_session_id.is_empty() {
        return source.source_session_id.clone();
    }
    for key in ["session_id", "thread_id"] {
        if let Some(id) = source.backend.get(key).and_then(Value::as_str) {
            if !id.is_empty() {
                return id.to_string();
            }
        }
    }
    String::new()
}

fn source_is_deleted(source: &SessionSummarySource, deleted: &DeletedSessionRefs) -> bool {
    deleted.ids.contains(&source.id)
        || deleted.ids.contains(&backend_session_id(source))
        || (!source.source_path.is_empty() && deleted.paths.contains(&source.source_path))
}

fn extract_user_text(content: &Value) -> String {
    match content {
        Value::String(text) => text.clone(),
        Value::Array(parts) => parts
            .iter()
            .filter_map(|part| {
                part.as_str()
                    .map(str::to_string)
                    .or_else(|| part.get("text").and_then(Value::as_str).map(str::to_string))
            })
            .collect::<Vec<_>>()
            .join(""),
        Value::Object(_) => content
            .get("text")
            .and_then(Value::as_str)
            .unwrap_or_default()
            .to_string(),
        _ => String::new(),
    }
}

fn first_text(item: &Value, keys: &[&str]) -> String {
    for key in keys {
        if let Some(text) = item.get(*key).and_then(Value::as_str) {
            if !text.trim().is_empty() {
                return text.trim().to_string();
            }
        }
    }
    String::new()
}

fn load_session(id: &str) -> Result<Value, String> {
    read_json(&session_path(id)?)
}

fn safe_artifact_session_key(id: &str) -> String {
    let label: String = id
        .chars()
        .map(|character| {
            if character.is_ascii_alphanumeric() || matches!(character, '.' | '_' | '-') {
                character
            } else {
                '-'
            }
        })
        .collect::<String>()
        .trim_matches('-')
        .chars()
        .take(48)
        .collect();
    let mut hasher = Sha1::new();
    hasher.update(id.as_bytes());
    let digest = format!("{:x}", hasher.finalize());
    format!("{}-{}", if label.is_empty() { "session" } else { &label }, &digest[..8])
}

fn artifact_session_dir(id: &str) -> Result<PathBuf, String> {
    validate_session_id(id)?;
    let root = artifacts_dir();
    let expected = safe_artifact_session_key(id);
    let expected_path = root.join(&expected);
    if expected_path.is_dir() {
        return Ok(expected_path);
    }
    if let Ok(entries) = fs::read_dir(&root) {
        let prefix = format!("{}-", id.replace(':', "-"));
        for entry in entries.filter_map(Result::ok) {
            let name = entry.file_name().to_string_lossy().to_string();
            if entry.path().is_dir() && name.starts_with(&prefix) {
                return Ok(entry.path());
            }
        }
    }
    fs::create_dir_all(&expected_path)
        .map_err(|error| format!("could not create {}: {error}", expected_path.display()))?;
    Ok(expected_path)
}

fn checked_artifact_path(session_id: &str, raw_path: &str) -> Result<PathBuf, String> {
    let directory = artifact_session_dir(session_id)?
        .canonicalize()
        .map_err(|error| format!("could not resolve artifact directory: {error}"))?;
    let path = PathBuf::from(shellexpand::tilde(raw_path).to_string())
        .canonicalize()
        .map_err(|error| format!("could not resolve artifact path: {error}"))?;
    if !path.starts_with(&directory) || !path.is_file() {
        return Err(String::from("artifact path is outside this session"));
    }
    Ok(path)
}

fn unique_destination(directory: &Path, name: &str) -> PathBuf {
    let source = Path::new(name);
    let stem = source
        .file_stem()
        .and_then(|value| value.to_str())
        .unwrap_or("artifact");
    let extension = source.extension().and_then(|value| value.to_str()).unwrap_or("");
    let mut candidate = directory.join(name);
    let mut suffix = 2;
    while candidate.exists() {
        let filename = if extension.is_empty() {
            format!("{stem} {suffix}")
        } else {
            format!("{stem} {suffix}.{extension}")
        };
        candidate = directory.join(filename);
        suffix += 1;
    }
    candidate
}

fn artifact_value(path: &Path) -> Value {
    let metadata = fs::metadata(path).ok();
    let extension = path
        .extension()
        .and_then(|value| value.to_str())
        .unwrap_or_default()
        .to_ascii_lowercase();
    let is_image = matches!(
        extension.as_str(),
        "png" | "jpg" | "jpeg" | "gif" | "webp" | "bmp" | "svg"
    );
    let is_text = matches!(
        extension.as_str(),
        "txt"
            | "md"
            | "json"
            | "jsonl"
            | "log"
            | "py"
            | "js"
            | "ts"
            | "tsx"
            | "jsx"
            | "css"
            | "html"
            | "yaml"
            | "yml"
            | "toml"
            | "ini"
            | "rs"
            | "sh"
    );
    json!({
        "name": path.file_name().and_then(|value| value.to_str()).unwrap_or_default(),
        "path": path.to_string_lossy(),
        "size": metadata.as_ref().map(|value| value.len()).unwrap_or(0),
        "modified": metadata
            .and_then(|value| value.modified().ok())
            .and_then(|value| value.duration_since(std::time::UNIX_EPOCH).ok())
            .map(|value| value.as_secs())
            .unwrap_or(0),
        "is_image": is_image,
        "is_text": is_text,
    })
}

#[tauri::command]
fn backend_status() -> Value {
    json!({
        "connected": true,
        "mode": "local-sessions"
    })
}

#[tauri::command]
fn get_app_state() -> Result<Value, String> {
    let config = read_json(&config_path()).unwrap_or_else(|_| json!({}));
    Ok(json!({
        "active_agent": config.get("active_agent").cloned().unwrap_or(Value::Null),
        "active_workspace_index": config.get("active_workspace_index").cloned().unwrap_or(json!(0)),
        "agent_settings": config.get("agent_settings").cloned().unwrap_or_else(|| json!({})),
        "claude_orchestration": config.get("claude_orchestration").cloned().unwrap_or_else(|| json!({"mode": "smart"})),
        "custom_clients": config.get("custom_clients").cloned().unwrap_or_else(|| json!([])),
        "cwd": config.get("cwd").cloned().unwrap_or_else(|| json!(home_dir())),
        "handoff_preferences": config.get("handoff_preferences").cloned().unwrap_or_else(|| json!({})),
        "recent_projects": config.get("recent_projects").cloned().unwrap_or_else(|| json!([home_dir()])),
        "workspace_tabs": config.get("workspace_tabs").cloned().unwrap_or_else(|| json!([])),
    }))
}

#[tauri::command]
fn save_workspace_state(tabs: Value, active_index: usize) -> Result<(), String> {
    let mut config = read_json(&config_path()).unwrap_or_else(|_| json!({}));
    let Some(object) = config.as_object_mut() else {
        return Err(String::from("config root is not an object"));
    };
    object.insert(String::from("workspace_tabs"), tabs);
    object.insert(String::from("active_workspace_index"), json!(active_index));
    write_json_atomic(&config_path(), &config)
}

#[tauri::command]
fn save_app_preferences(
    claude_orchestration: Value,
    handoff_preferences: Value,
) -> Result<(), String> {
    let mut config = read_json(&config_path()).unwrap_or_else(|_| json!({}));
    let Some(object) = config.as_object_mut() else {
        return Err(String::from("config root is not an object"));
    };
    object.insert(String::from("claude_orchestration"), claude_orchestration);
    object.insert(String::from("handoff_preferences"), handoff_preferences);
    write_json_atomic(&config_path(), &config)
}

#[tauri::command]
fn list_sessions() -> Result<Vec<SessionSummary>, String> {
    let deleted = deleted_session_refs();
    let mut sessions: Vec<SessionSummary> = Vec::new();
    let entries = fs::read_dir(sessions_dir())
        .map_err(|error| format!("could not list sessions: {error}"))?;

    for entry in entries.filter_map(Result::ok) {
        let path = entry.path();
        if path.extension().and_then(|extension| extension.to_str()) != Some("json") {
            continue;
        }
        let Ok(file) = fs::File::open(&path) else {
            continue;
        };
        let Ok(source) = serde_json::from_reader::<_, SessionSummarySource>(file) else {
            continue;
        };
        if !source.id.is_empty() && !source_is_deleted(&source, &deleted) {
            sessions.push(source.into());
        }
    }

    sessions.sort_by(|left, right| {
        let left_time = if left.updated_at.is_empty() {
            &left.created_at
        } else {
            &left.updated_at
        };
        let right_time = if right.updated_at.is_empty() {
            &right.created_at
        } else {
            &right.updated_at
        };
        right_time.cmp(left_time)
    });
    Ok(sessions)
}

#[tauri::command]
fn get_session(id: String) -> Result<Value, String> {
    load_session(&id)
}

#[tauri::command]
fn get_session_messages(id: String) -> Result<Value, String> {
    let session = load_session(&id)?;
    let agent = session.get("agent").and_then(Value::as_str).unwrap_or_default();
    let mut messages = Vec::new();
    for turn in session
        .get("turns")
        .and_then(Value::as_array)
        .into_iter()
        .flatten()
    {
        let turn_id = turn.get("id").and_then(Value::as_str).unwrap_or_default();
        let created_at = turn
            .get("created_at")
            .and_then(Value::as_str)
            .unwrap_or_default();
        let completed_at = turn
            .get("completed_at")
            .and_then(Value::as_str)
            .unwrap_or(created_at);
        for (index, item) in turn
            .get("items")
            .and_then(Value::as_array)
            .into_iter()
            .flatten()
            .enumerate()
        {
            match item.get("type").and_then(Value::as_str).unwrap_or_default() {
                "userMessage" => {
                    let text = extract_user_text(item.get("content").unwrap_or(&Value::Null));
                    if !text.trim().is_empty() {
                        messages.push(json!({
                            "id": format!("{turn_id}-user-{index}"),
                            "role": "user",
                            "content": text.trim(),
                            "time": created_at,
                        }));
                    }
                }
                "agentMessage" => {
                    let text = first_text(item, &["text", "content"]);
                    if !text.is_empty() {
                        messages.push(json!({
                            "id": format!("{turn_id}-agent-{index}"),
                            "role": "assistant",
                            "agent": agent,
                            "content": text,
                            "time": completed_at,
                        }));
                    }
                }
                _ => {}
            }
        }
    }
    Ok(Value::Array(messages))
}

#[tauri::command]
fn get_session_work_log(id: String) -> Result<Value, String> {
    let session = load_session(&id)?;
    let mut entries = Vec::new();
    for turn in session
        .get("turns")
        .and_then(Value::as_array)
        .into_iter()
        .flatten()
    {
        let turn_id = turn.get("id").and_then(Value::as_str).unwrap_or_default();
        let created_at = turn
            .get("created_at")
            .and_then(Value::as_str)
            .unwrap_or_default();
        for (index, item) in turn
            .get("items")
            .and_then(Value::as_array)
            .into_iter()
            .flatten()
            .enumerate()
        {
            let item_type = item.get("type").and_then(Value::as_str).unwrap_or_default();
            if matches!(item_type, "userMessage" | "agentMessage") {
                continue;
            }
            let content = match item_type {
                "commandExecution" => first_text(item, &["command", "aggregatedOutput", "text"]),
                "fileChange" => first_text(item, &["content", "text", "path"]),
                _ => first_text(item, &["content", "text", "message", "command"]),
            };
            if !content.is_empty() {
                entries.push(json!({
                    "id": format!("{turn_id}-work-{index}"),
                    "type": item_type,
                    "content": content,
                    "time": created_at,
                }));
            }
        }
    }
    Ok(Value::Array(entries))
}

#[tauri::command]
fn create_session(
    agent: String,
    model: String,
    effort: String,
    cwd: String,
    title: String,
) -> Result<Value, String> {
    let cwd_path = PathBuf::from(shellexpand::tilde(&cwd).to_string());
    if !cwd_path.is_dir() {
        return Err(format!("project directory does not exist: {}", cwd_path.display()));
    }
    let id = Uuid::new_v4().to_string();
    let timestamp = now_iso();
    let fallback_title = match agent.as_str() {
        "claude" => "New claude session",
        "codex" => "New codex session",
        "gemini" => "New gemini session",
        _ => "New local session",
    };
    let mut backend = json!({"model": model});
    if !effort.is_empty() && effort != "Default" && agent != "gemini" {
        backend["effort"] = json!(effort);
    }
    let session = json!({
        "id": id,
        "title": if title.trim().is_empty() { fallback_title } else { title.trim() },
        "agent": agent,
        "backend": backend,
        "cwd": cwd_path,
        "origin": "local",
        "created_at": timestamp,
        "updated_at": timestamp,
        "turns": [],
    });
    write_json_atomic(&session_path(session["id"].as_str().unwrap_or_default())?, &session)?;
    Ok(session)
}

#[tauri::command]
fn list_artifacts(session_id: String) -> Result<Value, String> {
    let directory = artifact_session_dir(&session_id)?;
    let mut artifacts = Vec::new();
    if let Ok(entries) = fs::read_dir(directory) {
        for entry in entries.filter_map(Result::ok) {
            if entry.path().is_file() {
                artifacts.push(artifact_value(&entry.path()));
            }
        }
    }
    artifacts.sort_by(|left, right| {
        right["modified"]
            .as_u64()
            .unwrap_or_default()
            .cmp(&left["modified"].as_u64().unwrap_or_default())
    });
    Ok(Value::Array(artifacts))
}

#[tauri::command]
fn store_artifacts(session_id: String, paths: Vec<String>) -> Result<Value, String> {
    let directory = artifact_session_dir(&session_id)?;
    let mut stored = Vec::new();
    for raw_path in paths {
        let source = PathBuf::from(shellexpand::tilde(&raw_path).to_string());
        if !source.is_file() {
            continue;
        }
        let name = source
            .file_name()
            .and_then(|value| value.to_str())
            .unwrap_or("artifact");
        let destination = unique_destination(&directory, name);
        fs::copy(&source, &destination).map_err(|error| {
            format!(
                "could not copy {} to {}: {error}",
                source.display(),
                destination.display()
            )
        })?;
        stored.push(artifact_value(&destination));
    }
    Ok(Value::Array(stored))
}

#[tauri::command]
fn rename_artifact(
    session_id: String,
    path: String,
    new_name: String,
) -> Result<Value, String> {
    let source = checked_artifact_path(&session_id, &path)?;
    let name = new_name.trim();
    if name.is_empty()
        || Path::new(name).file_name().and_then(|value| value.to_str()) != Some(name)
    {
        return Err(String::from("artifact name must be a plain file name"));
    }
    let directory = source
        .parent()
        .ok_or_else(|| String::from("artifact has no parent directory"))?;
    let destination = directory.join(name);
    if destination.exists() && destination != source {
        return Err(String::from("an artifact with that name already exists"));
    }
    fs::rename(&source, &destination).map_err(|error| {
        format!(
            "could not rename {} to {}: {error}",
            source.display(),
            destination.display()
        )
    })?;
    Ok(artifact_value(&destination))
}

#[tauri::command]
fn delete_artifact(session_id: String, path: String) -> Result<(), String> {
    let path = checked_artifact_path(&session_id, &path)?;
    fs::remove_file(&path)
        .map_err(|error| format!("could not delete {}: {error}", path.display()))
}

#[tauri::command]
fn paste_clipboard_image(session_id: String) -> Result<Value, String> {
    let types = Command::new("wl-paste")
        .arg("--list-types")
        .output()
        .map_err(|error| format!("could not inspect the Wayland clipboard: {error}"))?;
    if !types.status.success() {
        return Err(String::from("could not inspect the Wayland clipboard"));
    }
    let available = String::from_utf8_lossy(&types.stdout);
    let formats = [
        ("image/png", "png"),
        ("image/jpeg", "jpg"),
        ("image/webp", "webp"),
        ("image/gif", "gif"),
        ("image/bmp", "bmp"),
    ];
    let Some((mime, extension)) = formats
        .into_iter()
        .find(|(mime, _)| available.lines().any(|line| line.trim() == *mime))
    else {
        return Err(String::from("the clipboard does not contain an image"));
    };
    let image = Command::new("wl-paste")
        .args(["--type", mime])
        .output()
        .map_err(|error| format!("could not read the clipboard image: {error}"))?;
    if !image.status.success() || image.stdout.is_empty() {
        return Err(String::from("could not read the clipboard image"));
    }
    let directory = artifact_session_dir(&session_id)?;
    let filename = format!(
        "clipboard-{}.{}",
        OffsetDateTime::now_utc().unix_timestamp_nanos(),
        extension
    );
    let destination = directory.join(filename);
    fs::write(&destination, image.stdout)
        .map_err(|error| format!("could not save clipboard image: {error}"))?;
    Ok(artifact_value(&destination))
}

fn session_native_id(session: &Value) -> String {
    if let Some(id) = session.get("source_session_id").and_then(Value::as_str) {
        if !id.is_empty() {
            return id.to_string();
        }
    }
    let backend = session.get("backend").unwrap_or(&Value::Null);
    for key in ["session_id", "thread_id"] {
        if let Some(id) = backend.get(key).and_then(Value::as_str) {
            if !id.is_empty() {
                return id.to_string();
            }
        }
    }
    String::new()
}

fn claude_session_paths(session: &Value, native_id: &str) -> Vec<PathBuf> {
    let mut paths = Vec::new();
    if let Some(path) = session.get("source_path").and_then(Value::as_str) {
        let path = PathBuf::from(path);
        if path.is_file() {
            paths.push(path);
        }
    }
    let root = home_dir().join(".claude/projects");
    if let Ok(projects) = fs::read_dir(root) {
        for project in projects.filter_map(Result::ok) {
            if !project.path().is_dir() {
                continue;
            }
            let candidate = project.path().join(format!("{native_id}.jsonl"));
            if candidate.is_file() && !paths.contains(&candidate) {
                paths.push(candidate);
            }
        }
    }
    paths
}

fn append_unique(array: &mut Value, value: &str) {
    if value.is_empty() {
        return;
    }
    if !array.is_array() {
        *array = json!([]);
    }
    let values = array.as_array_mut().expect("array initialized above");
    if !values.iter().any(|item| item.as_str() == Some(value)) {
        values.push(json!(value));
    }
}

fn record_deleted_session(session: &Value) -> Result<(), String> {
    let agent = session
        .get("agent")
        .and_then(Value::as_str)
        .unwrap_or("local");
    let native_id = session_native_id(session);
    let source_path = session
        .get("source_path")
        .and_then(Value::as_str)
        .unwrap_or_default();
    let mut config = read_json(&config_path()).unwrap_or_else(|_| json!({}));
    let Some(config_object) = config.as_object_mut() else {
        return Err(String::from("config root is not an object"));
    };
    let tombstones = config_object
        .entry(String::from("deleted_session_tombstones"))
        .or_insert_with(|| json!({}));
    if !tombstones.is_object() {
        *tombstones = json!({});
    }
    let bucket = tombstones
        .as_object_mut()
        .expect("object initialized above")
        .entry(agent.to_string())
        .or_insert_with(|| json!({"ids": [], "paths": []}));
    if !bucket.is_object() {
        *bucket = json!({"ids": [], "paths": []});
    }
    let bucket = bucket.as_object_mut().expect("object initialized above");
    append_unique(
        bucket.entry(String::from("ids")).or_insert_with(|| json!([])),
        &native_id,
    );
    append_unique(
        bucket
            .entry(String::from("paths"))
            .or_insert_with(|| json!([])),
        source_path,
    );
    write_json_atomic(&config_path(), &config)
}

fn sync_native_title(session: &Value, title: &str) -> bool {
    let agent = session
        .get("agent")
        .and_then(Value::as_str)
        .unwrap_or_default();
    let native_id = session_native_id(session);
    if native_id.is_empty() {
        return false;
    }
    if agent == "claude" {
        let Some(path) = claude_session_paths(session, &native_id).into_iter().next() else {
            return false;
        };
        let records = [
            json!({"type": "custom-title", "customTitle": title, "sessionId": native_id}),
            json!({"type": "agent-name", "agentName": title, "sessionId": native_id}),
            json!({"type": "ai-title", "aiTitle": title, "sessionId": native_id}),
        ];
        let Ok(mut file) = OpenOptions::new().append(true).open(path) else {
            return false;
        };
        for record in records {
            let Ok(encoded) = serde_json::to_string(&record) else {
                return false;
            };
            if writeln!(file, "{encoded}").is_err() {
                return false;
            }
        }
        return file.sync_all().is_ok();
    }
    if agent == "codex" {
        let database = home_dir().join(".codex/state_5.sqlite");
        if !database.is_file() {
            return false;
        }
        let script = "import sqlite3,sys,time\ncon=sqlite3.connect(sys.argv[1])\ncon.execute('update threads set title=?, preview=?, updated_at=?, updated_at_ms=? where id=?',(sys.argv[3],sys.argv[3],int(time.time()),int(time.time()*1000),sys.argv[2]))\ncon.commit()\n";
        return Command::new("/usr/bin/python3")
            .args([
                "-c",
                script,
                &database.to_string_lossy(),
                &native_id,
                title,
            ])
            .status()
            .map(|status| status.success())
            .unwrap_or(false);
    }
    false
}

#[tauri::command]
fn rename_session(id: String, title: String) -> Result<Value, String> {
    let title = title.trim();
    if title.is_empty() {
        return Err(String::from("session title cannot be empty"));
    }
    let mut session = load_session(&id)?;
    session["title"] = json!(title);
    session["user_renamed"] = json!(true);
    session["updated_at"] = json!(now_iso());
    let native_updated = sync_native_title(&session, title);
    write_json_atomic(&session_path(&id)?, &session)?;
    Ok(json!({"session": session, "native_updated": native_updated}))
}

fn delete_native_session(session: &Value) -> Result<bool, String> {
    let agent = session
        .get("agent")
        .and_then(Value::as_str)
        .unwrap_or_default();
    let native_id = session_native_id(session);
    if native_id.is_empty() {
        return Ok(false);
    }
    if agent == "claude" {
        for path in claude_session_paths(session, &native_id) {
            fs::remove_file(&path)
                .map_err(|error| format!("could not delete {}: {error}", path.display()))?;
        }
        return Ok(true);
    }
    if agent == "codex" {
        let output = Command::new("codex")
            .args(["archive", &native_id])
            .output()
            .map_err(|error| format!("could not archive Codex session: {error}"))?;
        if !output.status.success() {
            return Err(String::from_utf8_lossy(&output.stdout).trim().to_string());
        }
        return Ok(true);
    }
    if agent == "gemini" {
        let cwd = session
            .get("cwd")
            .and_then(Value::as_str)
            .map(str::to_string)
            .unwrap_or_else(|| home_dir().to_string_lossy().to_string());
        let listed = Command::new("gemini")
            .args([
                "--list-sessions",
                "--skip-trust",
                "--yolo",
                "--include-directories",
                &cwd,
            ])
            .current_dir(&cwd)
            .output()
            .map_err(|error| format!("could not list Gemini sessions: {error}"))?;
        let listing = String::from_utf8_lossy(&listed.stdout);
        let index = listing.lines().find_map(|line| {
            if !line.contains(&format!("[{native_id}]")) {
                return None;
            }
            line.trim()
                .split_once('.')
                .map(|(index, _)| index.to_string())
        });
        let Some(index) = index else {
            return Err(String::from("Gemini session was not found in its native history"));
        };
        let deleted = Command::new("gemini")
            .args([
                "--delete-session",
                &index,
                "--skip-trust",
                "--yolo",
                "--include-directories",
                &cwd,
            ])
            .current_dir(&cwd)
            .output()
            .map_err(|error| format!("could not delete Gemini session: {error}"))?;
        if !deleted.status.success() {
            return Err(String::from_utf8_lossy(&deleted.stdout).trim().to_string());
        }
        return Ok(true);
    }
    Ok(false)
}

#[tauri::command]
fn delete_session(id: String) -> Result<Value, String> {
    let session = load_session(&id)?;
    let native_deleted = delete_native_session(&session)?;
    record_deleted_session(&session)?;
    let path = session_path(&id)?;
    if path.exists() {
        fs::remove_file(&path)
            .map_err(|error| format!("could not delete {}: {error}", path.display()))?;
    }
    Ok(json!({"native_deleted": native_deleted}))
}

#[tauri::command]
fn start_terminal(
    app: AppHandle,
    manager: State<TerminalManager>,
    workspace_id: String,
    cwd: String,
    cols: u16,
    rows: u16,
) -> Result<String, String> {
    if workspace_id.trim().is_empty() {
        return Err(String::from("workspace id is required"));
    }
    let cwd_path = PathBuf::from(shellexpand::tilde(&cwd).to_string());
    if !cwd_path.is_dir() {
        return Err(format!("terminal directory does not exist: {}", cwd_path.display()));
    }
    let size = PtySize {
        rows: rows.max(1),
        cols: cols.max(1),
        pixel_width: 0,
        pixel_height: 0,
    };
    let mut sessions = manager
        .sessions
        .lock()
        .map_err(|_| String::from("terminal manager lock is poisoned"))?;
    if let Some(session) = sessions.get(&workspace_id) {
        session
            .master
            .resize(size)
            .map_err(|error| format!("could not resize terminal: {error}"))?;
        return session
            .buffer
            .lock()
            .map(|buffer| buffer.clone())
            .map_err(|_| String::from("terminal buffer lock is poisoned"));
    }

    let pair = native_pty_system()
        .openpty(size)
        .map_err(|error| format!("could not open terminal: {error}"))?;
    let shell = std::env::var("SHELL").unwrap_or_else(|_| String::from("/bin/bash"));
    let mut command = CommandBuilder::new(shell);
    command.arg("-i");
    command.cwd(cwd_path);
    command.env("TERM", "xterm-256color");
    let child = pair
        .slave
        .spawn_command(command)
        .map_err(|error| format!("could not start shell: {error}"))?;
    drop(pair.slave);
    let mut reader = pair
        .master
        .try_clone_reader()
        .map_err(|error| format!("could not read terminal: {error}"))?;
    let writer = pair
        .master
        .take_writer()
        .map_err(|error| format!("could not write terminal: {error}"))?;
    let buffer = Arc::new(Mutex::new(String::new()));
    let output_buffer = Arc::clone(&buffer);
    let output_workspace_id = workspace_id.clone();
    std::thread::spawn(move || {
        let mut bytes = [0_u8; 8192];
        loop {
            let Ok(count) = reader.read(&mut bytes) else {
                break;
            };
            if count == 0 {
                break;
            }
            let data = String::from_utf8_lossy(&bytes[..count]).to_string();
            if let Ok(mut backlog) = output_buffer.lock() {
                backlog.push_str(&data);
                if backlog.len() > 1_000_000 {
                    let trim_at = backlog.len() - 750_000;
                    let boundary = backlog
                        .char_indices()
                        .find_map(|(index, _)| (index >= trim_at).then_some(index))
                        .unwrap_or(trim_at);
                    backlog.drain(..boundary);
                }
            }
            let _ = app.emit(
                "terminal-output",
                TerminalOutput {
                    workspace_id: output_workspace_id.clone(),
                    data,
                },
            );
        }
    });
    sessions.insert(
        workspace_id,
        TerminalSession {
            master: pair.master,
            writer,
            child,
            buffer,
        },
    );
    Ok(String::new())
}

#[tauri::command]
fn terminal_write(
    manager: State<TerminalManager>,
    workspace_id: String,
    data: String,
) -> Result<(), String> {
    let mut sessions = manager
        .sessions
        .lock()
        .map_err(|_| String::from("terminal manager lock is poisoned"))?;
    let session = sessions
        .get_mut(&workspace_id)
        .ok_or_else(|| String::from("terminal is not running"))?;
    session
        .writer
        .write_all(data.as_bytes())
        .and_then(|_| session.writer.flush())
        .map_err(|error| format!("could not write terminal input: {error}"))
}

#[tauri::command]
fn resize_terminal(
    manager: State<TerminalManager>,
    workspace_id: String,
    cols: u16,
    rows: u16,
) -> Result<(), String> {
    let sessions = manager
        .sessions
        .lock()
        .map_err(|_| String::from("terminal manager lock is poisoned"))?;
    let session = sessions
        .get(&workspace_id)
        .ok_or_else(|| String::from("terminal is not running"))?;
    session
        .master
        .resize(PtySize {
            rows: rows.max(1),
            cols: cols.max(1),
            pixel_width: 0,
            pixel_height: 0,
        })
        .map_err(|error| format!("could not resize terminal: {error}"))
}

#[tauri::command]
fn stop_terminal(
    manager: State<TerminalManager>,
    workspace_id: String,
) -> Result<(), String> {
    let mut sessions = manager
        .sessions
        .lock()
        .map_err(|_| String::from("terminal manager lock is poisoned"))?;
    if let Some(mut session) = sessions.remove(&workspace_id) {
        session
            .child
            .kill()
            .map_err(|error| format!("could not stop terminal: {error}"))?;
    }
    Ok(())
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .manage(TerminalManager::default())
        .plugin(tauri_plugin_dialog::init())
        .plugin(tauri_plugin_opener::init())
        .invoke_handler(tauri::generate_handler![
            backend_status,
            get_app_state,
            save_workspace_state,
            save_app_preferences,
            list_sessions,
            get_session,
            get_session_messages,
            get_session_work_log,
            create_session,
            rename_session,
            delete_session,
            list_artifacts,
            store_artifacts,
            rename_artifact,
            delete_artifact,
            paste_clipboard_image,
            start_terminal,
            terminal_write,
            resize_terminal,
            stop_terminal,
        ])
        .run(tauri::generate_context!())
        .expect("failed to run Agent Workbench Next");
}

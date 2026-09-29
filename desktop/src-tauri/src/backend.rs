use std::path::PathBuf;
use std::process::{Child, Command, Stdio};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Mutex;
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

const HOST: &str = "127.0.0.1";
const POLL_INTERVAL: Duration = Duration::from_millis(100);
const CREATE_NO_WINDOW: u32 = 0x08000000;

#[derive(Clone, Debug, serde::Serialize)]
pub struct BackendConnection {
    pub url: String,
}

#[derive(serde::Deserialize)]
struct ReadyInfo {
    host: String,
    port: u16,
}

// Every launch gets a newly created directory, so a stale readiness file from
// another process can never be mistaken for this child's address.
struct RuntimeDirectory(PathBuf);

impl RuntimeDirectory {
    fn create() -> Result<Self, String> {
        let timestamp = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .map_err(|e| e.to_string())?
            .as_nanos();
        for attempt in 0..100 {
            let path = std::env::temp_dir().join(format!(
                "galtransl-backend-{}-{timestamp}-{attempt}",
                std::process::id()
            ));
            match std::fs::create_dir(&path) {
                Ok(()) => return Ok(Self(path)),
                Err(e) if e.kind() == std::io::ErrorKind::AlreadyExists => continue,
                Err(e) => return Err(format!("创建后端启动目录失败: {e}")),
            }
        }
        Err("无法创建独立的后端启动目录".to_string())
    }

    fn ready_file(&self) -> PathBuf {
        self.0.join("ready.json")
    }
}

impl Drop for RuntimeDirectory {
    fn drop(&mut self) {
        let _ = std::fs::remove_file(self.ready_file());
        let _ = std::fs::remove_file(self.0.join("ready.json.tmp"));
        let _ = std::fs::remove_dir(&self.0);
    }
}

struct ManagedBackend {
    child: Child,
    runtime_dir: RuntimeDirectory,
    connection: BackendConnection,
}

impl ManagedBackend {
    fn stop(&mut self) -> Result<(), String> {
        if self.child.try_wait().map_err(|e| e.to_string())?.is_some() {
            return Ok(());
        }
        #[cfg(target_os = "windows")]
        {
            use std::os::windows::process::CommandExt;
            // PyInstaller and imported tools can have children. Scope cleanup
            // to the process tree we actually spawned, never an executable name.
            let status = Command::new("taskkill")
                .args(["/F", "/T", "/PID", &self.child.id().to_string()])
                .creation_flags(CREATE_NO_WINDOW)
                .stdout(Stdio::null())
                .stderr(Stdio::null())
                .status()
                .map_err(|e| format!("停止本实例后端失败: {e}"))?;
            if !status.success() && self.child.try_wait().map_err(|e| e.to_string())?.is_none() {
                return Err(format!("停止本实例后端失败（PID {}）", self.child.id()));
            }
        }
        #[cfg(not(target_os = "windows"))]
        self.child.kill().map_err(|e| e.to_string())?;
        self.child.wait().map_err(|e| e.to_string())?;
        Ok(())
    }
}

impl Drop for ManagedBackend {
    fn drop(&mut self) {
        let _ = self.stop();
    }
}

#[derive(Default)]
pub struct BackendManager {
    managed: Mutex<Option<ManagedBackend>>,
    shutting_down: AtomicBool,
}

impl BackendManager {
    pub fn ensure(
        &self,
        hide_console: bool,
        timeout: Duration,
    ) -> Result<BackendConnection, String> {
        self.ensure_with(hide_console, timeout, || {
            let path = backend_executable_candidates()
                .into_iter()
                .find(|candidate| candidate.is_file())
                .ok_or("未找到可用的服务端可执行文件 galtransl_backend.exe")?;
            let mut command = Command::new(&path);
            if let Some(parent) = path.parent() {
                let working_dir = if parent.file_name().is_some_and(|name| name == "backend") {
                    parent.parent().unwrap_or(parent)
                } else {
                    parent
                };
                command.current_dir(working_dir);
            }
            Ok(command)
        })
    }

    fn ensure_with(
        &self,
        hide_console: bool,
        timeout: Duration,
        command: impl FnOnce() -> Result<Command, String>,
    ) -> Result<BackendConnection, String> {
        // Serialize prewarming and frontend retries inside this desktop process.
        // Other desktop processes have separate managers and separate children.
        let mut slot = self.managed.lock().map_err(|_| "后端进程状态锁定失败")?;
        if self.shutting_down.load(Ordering::SeqCst) {
            return Err("桌面窗口正在关闭，已取消后端启动".to_string());
        }
        if let Some(managed) = slot.as_mut() {
            if managed
                .child
                .try_wait()
                .map_err(|e| e.to_string())?
                .is_none()
            {
                return Ok(managed.connection.clone());
            }
        }
        *slot = None;

        let runtime_dir = RuntimeDirectory::create()?;
        let mut command = command()?;
        command.args(["--host", HOST, "--port", "0", "--ready-file"]);
        command.arg(runtime_dir.ready_file());
        #[cfg(target_os = "windows")]
        {
            use std::os::windows::process::CommandExt;
            if hide_console {
                command.creation_flags(CREATE_NO_WINDOW);
            }
        }
        #[cfg(not(target_os = "windows"))]
        let _ = hide_console;
        let child = command
            .spawn()
            .map_err(|e| format!("启动服务端失败: {e}"))?;
        let mut managed = ManagedBackend {
            child,
            runtime_dir,
            connection: BackendConnection { url: String::new() },
        };
        let deadline = Instant::now() + timeout;
        loop {
            if self.shutting_down.load(Ordering::SeqCst) {
                return Err("桌面窗口正在关闭，已取消后端启动".to_string());
            }
            if let Some(status) = managed.child.try_wait().map_err(|e| e.to_string())? {
                return Err(format!(
                    "本地后端在启动完成前退出（{status}），请检查后端控制台"
                ));
            }
            match std::fs::read(managed.runtime_dir.ready_file()) {
                Ok(bytes) => {
                    let ready: ReadyInfo = serde_json::from_slice(&bytes)
                        .map_err(|e| format!("后端返回的启动信息无效: {e}"))?;
                    if ready.host != HOST || ready.port == 0 {
                        return Err("后端返回了无效的本地监听地址".to_string());
                    }
                    managed.connection.url = format!("http://{}:{}", ready.host, ready.port);
                    let connection = managed.connection.clone();
                    *slot = Some(managed);
                    return Ok(connection);
                }
                Err(e) if e.kind() == std::io::ErrorKind::NotFound => {}
                Err(e) => return Err(format!("读取后端启动信息失败: {e}")),
            }
            if Instant::now() >= deadline {
                // ManagedBackend's Drop stops this child even on timeout/error.
                return Err(format!(
                    "等待本地后端启动超时（{} ms）",
                    timeout.as_millis()
                ));
            }
            std::thread::sleep(POLL_INTERVAL);
        }
    }

    pub fn shutdown(&self) -> Result<(), String> {
        // Signal first so closing during startup interrupts the readiness wait.
        self.shutting_down.store(true, Ordering::SeqCst);
        let mut slot = self.managed.lock().map_err(|_| "后端进程状态锁定失败")?;
        if let Some(managed) = slot.as_mut() {
            managed.stop()?;
        }
        *slot = None;
        Ok(())
    }
}

fn backend_executable_candidates() -> Vec<PathBuf> {
    let Ok(current_exe) = std::env::current_exe() else {
        return Vec::new();
    };
    let Some(exe_dir) = current_exe.parent() else {
        return Vec::new();
    };
    let mut candidates = Vec::new();
    for dir in exe_dir.ancestors() {
        candidates.push(dir.join("backend/galtransl_backend.exe"));
        candidates.push(dir.join("dist/galtransl_backend/galtransl_backend.exe"));
        candidates.push(dir.join("dist/galtransl_backend.exe"));
        candidates.push(dir.join("galtransl_backend.exe"));
    }
    candidates
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::{Read, Write};
    use std::net::{SocketAddr, TcpStream};
    use std::sync::atomic::AtomicUsize;

    fn python_command() -> Command {
        let root = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../..");
        let python = std::env::var_os("GALTRANSL_TEST_PYTHON")
            .map(PathBuf::from)
            .unwrap_or_else(|| {
                root.join(if cfg!(windows) {
                    ".venv/Scripts/python.exe"
                } else {
                    ".venv/bin/python"
                })
            });
        let mut command = Command::new(python);
        command
            .current_dir(root)
            .stdout(Stdio::null())
            .stderr(Stdio::null());
        command
    }

    fn backend_command() -> Result<Command, String> {
        let mut command = python_command();
        command.arg("run_backend.py");
        Ok(command)
    }

    fn assert_responds(connection: &BackendConnection) {
        let address = connection.url.trim_start_matches("http://");
        let mut stream = TcpStream::connect(address).unwrap();
        stream
            .set_read_timeout(Some(Duration::from_secs(5)))
            .unwrap();
        write!(
            stream,
            "GET /api/version HTTP/1.0\r\nHost: localhost\r\n\r\n"
        )
        .unwrap();
        let mut response = String::new();
        stream.read_to_string(&mut response).unwrap();
        assert!(response.contains("200 OK"), "{response}");
        assert!(response.contains("\"version\""), "{response}");
    }

    #[test]
    #[ignore = "requires the repository Python environment"]
    fn concurrent_instances_have_independent_ports_and_shutdown() {
        let first = BackendManager::default();
        let second = BackendManager::default();
        let (a, b) = std::thread::scope(|scope| {
            let a = scope.spawn(|| {
                first
                    .ensure_with(true, Duration::from_secs(20), backend_command)
                    .unwrap()
            });
            let b = scope.spawn(|| {
                second
                    .ensure_with(true, Duration::from_secs(20), backend_command)
                    .unwrap()
            });
            (a.join().unwrap(), b.join().unwrap())
        });
        assert_ne!(a.url, b.url);
        assert_responds(&a);
        assert_responds(&b);
        let first_dir = first
            .managed
            .lock()
            .unwrap()
            .as_ref()
            .unwrap()
            .runtime_dir
            .0
            .clone();
        first.shutdown().unwrap();
        assert!(!first_dir.exists());
        assert!(TcpStream::connect(a.url.trim_start_matches("http://")).is_err());
        assert_responds(&b);
        assert_eq!(
            second
                .ensure_with(true, Duration::from_secs(20), || panic!(
                    "must reuse own child"
                ))
                .unwrap()
                .url,
            b.url
        );
        second.shutdown().unwrap();
    }

    #[test]
    #[ignore = "requires the repository Python environment"]
    fn concurrent_requests_in_one_instance_share_one_child() {
        let manager = BackendManager::default();
        let spawns = AtomicUsize::new(0);
        let start = || {
            manager
                .ensure_with(true, Duration::from_secs(20), || {
                    spawns.fetch_add(1, Ordering::SeqCst);
                    backend_command()
                })
                .unwrap()
        };
        let (a, b) = std::thread::scope(|scope| {
            let a = scope.spawn(start);
            let b = scope.spawn(start);
            (a.join().unwrap(), b.join().unwrap())
        });
        assert_eq!(a.url, b.url);
        assert_eq!(spawns.load(Ordering::SeqCst), 1);
        assert_responds(&a);
        manager.shutdown().unwrap();
    }

    #[test]
    #[ignore = "requires the repository Python environment"]
    fn failed_start_can_be_retried() {
        let manager = BackendManager::default();
        let error = manager
            .ensure_with(true, Duration::from_secs(5), || {
                let mut command = python_command();
                command.args(["-c", "raise SystemExit(7)"]);
                Ok(command)
            })
            .unwrap_err();
        assert!(error.contains("启动完成前退出"), "{error}");
        let connection = manager
            .ensure_with(true, Duration::from_secs(20), backend_command)
            .unwrap();
        assert_responds(&connection);
        manager.shutdown().unwrap();
    }

    fn stalled_command(probe: &RuntimeDirectory) -> Result<Command, String> {
        let mut command = python_command();
        command.args(["-c", "import json,socket,sys,time; from pathlib import Path; s=socket.socket(); s.bind(('127.0.0.1',0)); s.listen(); Path(sys.argv[1]).write_text(json.dumps({'host':'127.0.0.1','port':s.getsockname()[1]})); time.sleep(30)"]);
        // Publish the test listener separately, never the launcher's ready file.
        command.arg(probe.ready_file());
        Ok(command)
    }

    fn probe_address(probe: &RuntimeDirectory) -> SocketAddr {
        let info: ReadyInfo =
            serde_json::from_slice(&std::fs::read(probe.ready_file()).unwrap()).unwrap();
        format!("{}:{}", info.host, info.port).parse().unwrap()
    }

    #[test]
    #[ignore = "requires the repository Python environment"]
    fn timeout_stops_only_the_spawned_process_tree() {
        let manager = BackendManager::default();
        let probe = RuntimeDirectory::create().unwrap();
        let error = manager
            .ensure_with(true, Duration::from_secs(2), || stalled_command(&probe))
            .unwrap_err();
        assert!(error.contains("超时"), "{error}");
        assert!(
            TcpStream::connect_timeout(&probe_address(&probe), Duration::from_millis(250)).is_err()
        );
    }

    #[test]
    #[ignore = "requires the repository Python environment"]
    fn closing_during_startup_cancels_and_cleans_up() {
        let manager = BackendManager::default();
        let probe = RuntimeDirectory::create().unwrap();
        std::thread::scope(|scope| {
            let startup = scope.spawn(|| {
                manager.ensure_with(true, Duration::from_secs(20), || stalled_command(&probe))
            });
            let deadline = Instant::now() + Duration::from_secs(5);
            while !probe.ready_file().exists() && Instant::now() < deadline {
                std::thread::sleep(POLL_INTERVAL);
            }
            manager.shutdown().unwrap();
            assert!(startup.join().unwrap().unwrap_err().contains("已取消"));
        });
        assert!(
            TcpStream::connect_timeout(&probe_address(&probe), Duration::from_millis(250)).is_err()
        );
        assert!(manager
            .ensure_with(true, Duration::from_secs(5), || panic!(
                "closed window cannot spawn"
            ))
            .is_err());
    }
}

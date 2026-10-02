#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

mod backend;

use std::path::PathBuf;
use std::sync::OnceLock;
use std::time::{Duration, Instant};

use backend::{BackendConnection, BackendManager};
use tauri::Manager;

const CREATE_NO_WINDOW: u32 = 0x08000000;
const BACKEND_STARTUP_TIMEOUT_MS: u64 = 20_000;

fn backend_manager() -> &'static BackendManager {
    static MANAGER: OnceLock<BackendManager> = OnceLock::new();
    MANAGER.get_or_init(BackendManager::default)
}

fn ensure_backend_ready_inner(resource_dir: Option<PathBuf>, hide_console: bool, timeout_ms: Option<u64>) -> Result<BackendConnection, String> {
    let timeout = Duration::from_millis(timeout_ms.unwrap_or(BACKEND_STARTUP_TIMEOUT_MS).min(120_000));
    if cfg!(debug_assertions) {
        // Development uses the Python process owned by run_desktop_dev.bat.
        // It is external to this window and must survive closing the dev shell.
        let address = std::net::SocketAddr::from(([127, 0, 0, 1], 12333));
        let deadline = Instant::now() + timeout;
        loop {
            if std::net::TcpStream::connect_timeout(&address, Duration::from_millis(250)).is_ok() {
                return Ok(BackendConnection { url: format!("http://{address}") });
            }
            if Instant::now() >= deadline {
                return Err("未连接到开发后端，请先运行 run_desktop_dev.bat 或启动 run_backend.py".to_string());
            }
            std::thread::sleep(Duration::from_millis(100));
        }
    }
    backend_manager().ensure(resource_dir.as_deref(), hide_console, timeout)
}

#[tauri::command]
async fn ensure_backend_ready(app: tauri::AppHandle, hide_console: Option<bool>, timeout_ms: Option<u64>) -> Result<BackendConnection, String> {
    let resource_dir = app.path().resource_dir().ok();
    tauri::async_runtime::spawn_blocking(move || {
        ensure_backend_ready_inner(resource_dir, hide_console.unwrap_or(true), timeout_ms)
    })
    .await
    .map_err(|e| format!("后端启动任务失败: {e}"))?
}

/// 「隐藏服务端控制台」这个偏好的落盘位置。
///
/// 前端把它存在 localStorage 里，Rust 读不到；但预热发生在窗口刚创建、
/// 网页还没加载完的时候（那时前端还没机会开口），所以让前端每次改这个
/// 开关时顺手同步一份过来，下次启动的预热就能沿用同样的窗口策略。
fn console_preference_path(app: &tauri::AppHandle) -> Option<PathBuf> {
    app.path()
        .app_config_dir()
        .ok()
        .map(|dir| dir.join("backend-console-preference"))
}

fn read_console_preference(app: &tauri::AppHandle) -> bool {
    console_preference_path(app)
        .and_then(|path| std::fs::read_to_string(path).ok())
        .map(|value| !matches!(value.trim(), "false" | "0"))
        .unwrap_or(true)
}

/// 由前端在切换「隐藏服务端控制台」时调用；写盘失败无所谓（只影响下次预热的窗口策略）。
#[tauri::command]
fn set_backend_console_preference(app: tauri::AppHandle, hide_console: bool) {
    let Some(path) = console_preference_path(&app) else {
        return;
    };
    if let Some(parent) = path.parent() {
        let _ = std::fs::create_dir_all(parent);
    }
    let _ = std::fs::write(path, if hide_console { "true" } else { "false" });
}

#[cfg(target_os = "windows")]
/// 使用 Windows Shell API 打开 Explorer：
/// - 当 path 指向目录时：打开该目录（若已有同路径 Explorer 窗口则复用并激活）
/// - 当 path 指向文件时：打开其父目录并滚动/高亮选中该文件（VSCode 风格）
fn windows_shell_open(path: &str) -> Result<(), String> {
    use windows::core::PCWSTR;
    use windows::Win32::System::Com::{
        CoInitializeEx, CoUninitialize, COINIT_APARTMENTTHREADED, COINIT_DISABLE_OLE1DDE,
    };
    use windows::Win32::UI::Shell::{ILCreateFromPathW, ILFree, SHOpenFolderAndSelectItems};

    let win_path = path.replace('/', "\\");
    let wide: Vec<u16> = win_path.encode_utf16().chain(std::iter::once(0)).collect();

    unsafe {
        let _ = CoInitializeEx(None, COINIT_APARTMENTTHREADED | COINIT_DISABLE_OLE1DDE);

        let pidl = ILCreateFromPathW(PCWSTR(wide.as_ptr()));
        if pidl.is_null() {
            CoUninitialize();
            return Err(format!("无法解析路径: {}", win_path));
        }

        let hr = SHOpenFolderAndSelectItems(pidl, None, 0);

        ILFree(Some(pidl));
        CoUninitialize();

        if let Err(e) = hr {
            return Err(format!("打开 Explorer 失败: {}", e));
        }
    }
    Ok(())
}

#[cfg(target_os = "windows")]
fn windows_explorer_select(path: &str) -> Result<(), String> {
    use std::os::windows::process::CommandExt;

    let win_path = path.replace('/', "\\");
    std::process::Command::new("explorer")
        .arg("/select,")
        .arg(&win_path)
        .creation_flags(CREATE_NO_WINDOW)
        .spawn()
        .map_err(|e| format!("定位文件失败: {}", e))?;
    Ok(())
}

#[tauri::command]
fn open_folder(path: String) -> Result<(), String> {
    #[cfg(target_os = "windows")]
    {
        use std::os::windows::process::CommandExt;

        // explorer 打开目录时默认会复用已经显示该路径的窗口（除非用户关闭了
        // “在不同窗口中打开文件夹”选项）。不要用 SHOpenFolderAndSelectItems，
        // 否则会在父目录中把该文件夹“选中”而不是进入它。
        let win_path = path.replace('/', "\\");
        std::process::Command::new("explorer")
            .arg(&win_path)
            .creation_flags(CREATE_NO_WINDOW)
            .spawn()
            .map_err(|e| format!("打开文件夹失败: {}", e))?;
    }
    #[cfg(target_os = "macos")]
    {
        std::process::Command::new("open")
            .arg(&path)
            .spawn()
            .map_err(|e| format!("打开文件夹失败: {}", e))?;
    }
    #[cfg(target_os = "linux")]
    {
        std::process::Command::new("xdg-open")
            .arg(&path)
            .spawn()
            .map_err(|e| format!("打开文件夹失败: {}", e))?;
    }
    Ok(())
}

#[tauri::command]
fn reveal_file(path: String) -> Result<(), String> {
    #[cfg(target_os = "windows")]
    {
        if let Err(shell_error) = windows_shell_open(&path) {
            windows_explorer_select(&path)
                .map_err(|fallback_error| format!("{}；备用方式也失败: {}", shell_error, fallback_error))?;
        }
        return Ok(());
    }
    #[cfg(target_os = "macos")]
    {
        std::process::Command::new("open")
            .arg("-R")
            .arg(&path)
            .spawn()
            .map_err(|e| format!("定位文件失败: {}", e))?;
    }
    #[cfg(target_os = "linux")]
    {
        let parent = std::path::Path::new(&path)
            .parent()
            .map(|p| p.to_string_lossy().to_string())
            .unwrap_or_else(|| path.clone());
        std::process::Command::new("xdg-open")
            .arg(&parent)
            .spawn()
            .map_err(|e| format!("定位文件失败: {}", e))?;
    }
    #[cfg(not(target_os = "windows"))]
    {
        Ok(())
    }
}

#[tauri::command]
fn create_dir(path: String) -> Result<(), String> {
    std::fs::create_dir_all(&path).map_err(|e| format!("创建目录失败: {}", e))
}

#[tauri::command]
fn write_text_file(path: String, content: String) -> Result<(), String> {
    std::fs::write(&path, content).map_err(|e| format!("写入文件失败: {}", e))
}

#[tauri::command]
fn copy_files(sources: Vec<String>, destination_dir: String) -> Result<(), String> {
    std::fs::create_dir_all(&destination_dir).map_err(|e| format!("创建目录失败: {}", e))?;
    for src in &sources {
        let file_name = std::path::Path::new(src)
            .file_name()
            .ok_or_else(|| format!("无效的文件路径: {}", src))?
            .to_string_lossy()
            .to_string();
        let dest = std::path::Path::new(&destination_dir).join(&file_name);
        std::fs::copy(src, &dest).map_err(|e| format!("复制文件失败: {} → {} ({})", src, dest.display(), e))?;
    }
    Ok(())
}

fn main() {
    tauri::Builder::default()
        .plugin(tauri_plugin_shell::init())
        .plugin(tauri_plugin_dialog::init())
        .plugin(tauri_plugin_notification::init())
        .invoke_handler(tauri::generate_handler![
            ensure_backend_ready,
            set_backend_console_preference,
            open_folder,
            reveal_file,
            create_dir,
            write_text_file,
            copy_files,
        ])
        .on_window_event(|_window, event| {
            if matches!(event, tauri::WindowEvent::Destroyed) {
                let _ = backend_manager().shutdown();
            }
        })
        .setup(|app| {
            // 预热：窗口还在加载网页时就把 Python 后端拉起来。前端的启动界面
            // 会一直盖着，等这里跑完再淡出——于是 Python 的启动时间被藏进了
            // 加载动画里，而不是等网页加载完才开始倒数。
            // 前端稍后调 ensure_backend_ready 时会直接复用这个进程。
            let hide_console = read_console_preference(app.handle());
            let resource_dir = app.path().resource_dir().ok();
            std::thread::spawn(move || {
                let _ = ensure_backend_ready_inner(resource_dir, hide_console, None);
            });
            Ok(())
        })
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}

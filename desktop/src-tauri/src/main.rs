#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use tauri::Manager;
use std::sync::{Mutex, OnceLock};

const BACKEND_HOST: &str = "127.0.0.1";
const BACKEND_PORT: u16 = 12333;

#[allow(dead_code)]
struct ManagedBackend {
    child: std::process::Child,
    path: String,
    port: u16,
}

fn managed_backend_slot() -> &'static Mutex<Option<ManagedBackend>> {
    static SLOT: OnceLock<Mutex<Option<ManagedBackend>>> = OnceLock::new();
    SLOT.get_or_init(|| Mutex::new(None))
}

fn tcp_port_listening(port: u16) -> bool {
    use std::net::TcpStream;
    use std::time::Duration;

    TcpStream::connect_timeout(
        &std::net::SocketAddr::from(([127, 0, 0, 1], port)),
        Duration::from_millis(250),
    )
    .is_ok()
}

fn shutdown_managed_backend_inner() -> Result<bool, String> {
    #[cfg(target_os = "windows")]
    {
        use std::os::windows::process::CommandExt;
        let mut cmd = std::process::Command::new("taskkill");
        cmd.args(["/F", "/IM", "galtransl_backend.exe"]);
        cmd.creation_flags(0x08000000); // CREATE_NO_WINDOW
        let output = cmd.output().map_err(|e| format!("执行 taskkill 失败: {}", e))?;

        if output.status.success() {
            let slot = managed_backend_slot();
            if let Ok(mut guard) = slot.lock() {
                *guard = None;
            }
            return Ok(true);
        } else {
            // 如果没有找到进程，taskkill 会返回错误，但这不算失败
            let stderr = String::from_utf8_lossy(&output.stderr);
            if stderr.contains("未找到") || stderr.contains("not found") {
                let slot = managed_backend_slot();
                if let Ok(mut guard) = slot.lock() {
                    *guard = None;
                }
                return Ok(false);
            }
            return Err(format!("杀掉后端进程失败: {}", stderr));
        }
    }

    #[cfg(not(target_os = "windows"))]
    {
        let slot = managed_backend_slot();
        let mut guard = slot.lock().map_err(|_| "后端进程状态锁定失败".to_string())?;
        let Some(mut managed) = guard.take() else {
            return Ok(false);
        };

        let _ = managed.child.kill();
        let _ = managed.child.wait();
        Ok(true)
    }
}

fn backend_executable_candidates() -> Vec<std::path::PathBuf> {
    let mut candidates = Vec::new();
    let mut seen = std::collections::HashSet::new();
    let backend_name = "galtransl_backend.exe";

    let current_exe = match std::env::current_exe() {
        Ok(path) => path,
        Err(_) => return candidates,
    };

    let exe_dir = match current_exe.parent() {
        Some(path) => path.to_path_buf(),
        None => return candidates,
    };

    for dir in std::iter::once(exe_dir.as_path()).chain(exe_dir.ancestors()) {
        let backend_in_release = dir.join("backend").join(backend_name);
        if seen.insert(backend_in_release.clone()) {
            candidates.push(backend_in_release);
        }

        let backend_in_dist = dir.join("dist").join(backend_name);
        if seen.insert(backend_in_dist.clone()) {
            candidates.push(backend_in_dist);
        }

        let backend_same_dir = dir.join(backend_name);
        if seen.insert(backend_same_dir.clone()) {
            candidates.push(backend_same_dir);
        }
    }

    candidates
}

/// 启动时拉起后端（仅一次）。若端口已监听则跳过；若未找到 exe 则静默跳过。
fn spawn_backend_on_startup() {
    let slot = managed_backend_slot();
    let Ok(mut guard) = slot.lock() else { return };

    if guard.is_some() {
        return;
    }

    if tcp_port_listening(BACKEND_PORT) {
        return;
    }

    let Some(path) = backend_executable_candidates()
        .into_iter()
        .find(|candidate| candidate.exists())
    else {
        return;
    };

    let mut command = std::process::Command::new(&path);
    command
        .arg("--host")
        .arg(BACKEND_HOST)
        .arg("--port")
        .arg(BACKEND_PORT.to_string());

    #[cfg(target_os = "windows")]
    {
        use std::os::windows::process::CommandExt;
        // 始终隐藏控制台窗口（CREATE_NO_WINDOW）
        command.creation_flags(0x08000000);
    }

    if let Ok(child) = command.spawn() {
        *guard = Some(ManagedBackend {
            child,
            path: path.to_string_lossy().to_string(),
            port: BACKEND_PORT,
        });
    }
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

        hr.map_err(|e| format!("打开 Explorer 失败: {}", e))?;
    }
    Ok(())
}

#[tauri::command]
fn open_folder(path: String) -> Result<(), String> {
    #[cfg(target_os = "windows")]
    {
        // explorer 打开目录时默认会复用已经显示该路径的窗口（除非用户关闭了
        // “在不同窗口中打开文件夹”选项）。不要用 SHOpenFolderAndSelectItems，
        // 否则会在父目录中把该文件夹“选中”而不是进入它。
        let win_path = path.replace('/', "\\");
        std::process::Command::new("explorer")
            .arg(&win_path)
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
        return windows_shell_open(&path);
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
        .invoke_handler(tauri::generate_handler![
            open_folder,
            reveal_file,
            create_dir,
            write_text_file,
            copy_files,
        ])
        .on_window_event(|_window, event| {
            if matches!(event, tauri::WindowEvent::Destroyed) {
                let _ = shutdown_managed_backend_inner();
            }
        })
        .setup(|app| {
            // 启动时先拉起后端（仅一次），再显示前端窗口
            spawn_backend_on_startup();
            #[cfg(debug_assertions)]
            {
                let webview = app.get_webview_window("main").expect("main window");
                webview.show()?;
            }
            Ok(())
        })
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}

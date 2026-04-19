#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use tauri::Manager;

#[derive(serde::Serialize)]
struct EnsureBackendResult {
    found: bool,
    started: bool,
    path: String,
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

#[tauri::command]
fn ensure_backend_running(hide_console: bool) -> Result<EnsureBackendResult, String> {
    let backend_path = backend_executable_candidates()
        .into_iter()
        .find(|candidate| candidate.exists());

    let Some(path) = backend_path else {
        return Ok(EnsureBackendResult {
            found: false,
            started: false,
            path: String::new(),
        });
    };

    let mut command = std::process::Command::new(&path);

    #[cfg(target_os = "windows")]
    {
        use std::os::windows::process::CommandExt;
        if hide_console {
            command.creation_flags(0x08000000);
        }
    }

    command
        .spawn()
        .map_err(|e| format!("启动服务端失败: {}", e))?;

    Ok(EnsureBackendResult {
        found: true,
        started: true,
        path: path.to_string_lossy().to_string(),
    })
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
            ensure_backend_running
        ])
        .setup(|app| {
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

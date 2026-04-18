#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use tauri::Manager;
use tauri_plugin_shell::ShellExt;

#[tauri::command]
fn open_folder(path: String) -> Result<(), String> {
    #[cfg(target_os = "windows")]
    {
        std::process::Command::new("explorer")
            .arg(&path)
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
        use windows::core::PCWSTR;
        use windows::Win32::System::Com::{
            CoInitializeEx, CoUninitialize, COINIT_APARTMENTTHREADED, COINIT_DISABLE_OLE1DDE,
        };
        use windows::Win32::UI::Shell::{ILCreateFromPathW, ILFree, SHOpenFolderAndSelectItems};

        // Normalize path separator; Shell API requires backslashes.
        let win_path = path.replace('/', "\\");
        let wide: Vec<u16> = win_path.encode_utf16().chain(std::iter::once(0)).collect();

        unsafe {
            // 初始化 COM（若已初始化会返回 S_FALSE，无需处理）
            let _ = CoInitializeEx(
                None,
                COINIT_APARTMENTTHREADED | COINIT_DISABLE_OLE1DDE,
            );

            let pidl = ILCreateFromPathW(PCWSTR(wide.as_ptr()));
            if pidl.is_null() {
                CoUninitialize();
                return Err("无法解析文件路径".into());
            }

            // 传入文件自身的 PIDL、apidl=None、flags=0：
            // 会复用已经在父目录的 Explorer 窗口，并滚动/高亮选中该文件（与 VSCode 的 Reveal 一致）。
            let hr = SHOpenFolderAndSelectItems(pidl, None, 0);

            ILFree(Some(pidl));
            CoUninitialize();

            hr.map_err(|e| format!("定位文件失败: {}", e))?;
        }
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
    Ok(())
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
        std::fs::copy(src, &dest).map_err(|e| format!("复制文件失败: {} → {}", src, dest.display()))?;
    }
    Ok(())
}

fn main() {
    tauri::Builder::default()
        .plugin(tauri_plugin_shell::init())
        .plugin(tauri_plugin_dialog::init())
        .invoke_handler(tauri::generate_handler![open_folder, reveal_file, create_dir, write_text_file, copy_files])
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

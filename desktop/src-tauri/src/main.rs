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
        .invoke_handler(tauri::generate_handler![open_folder, create_dir, write_text_file, copy_files])
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

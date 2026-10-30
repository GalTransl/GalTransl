use std::fs::File;
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::time::{Duration, Instant};

#[derive(serde::Deserialize)]
struct UpdatePlan {
    install_dir: PathBuf,
    executable: PathBuf,
    payload_dir: PathBuf,
}

pub fn launch_installer(update_id: &str) -> Result<(), String> {
    let executable = std::env::current_exe().map_err(|error| error.to_string())?;
    let stage = validate_stage(update_id, &executable)?;
    let ready = stage.join("installer-ready");
    if ready.exists() {
        std::fs::remove_file(&ready).map_err(|error| error.to_string())?;
    }
    let log = File::create(stage.join("installer.log")).map_err(|error| error.to_string())?;
    let helper = stage.join("helper").join(if cfg!(windows) {
        "galtransl_backend.exe"
    } else {
        "galtransl_backend"
    });
    let mut command = Command::new(helper);
    command
        .arg("--apply-update")
        .arg(stage.join("plan.json"))
        .arg("--parent-pid")
        .arg(std::process::id().to_string())
        .current_dir(&stage)
        // A copied PyInstaller executable needs its own extracted runtime.
        .env("PYINSTALLER_RESET_ENVIRONMENT", "1")
        .stdin(Stdio::null())
        .stdout(log.try_clone().map_err(|error| error.to_string())?)
        .stderr(log);
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        command.creation_flags(0x08000000 | 0x00000200);
    }
    #[cfg(unix)]
    {
        use std::os::unix::process::CommandExt;
        command.process_group(0);
    }
    let mut child = command.spawn().map_err(|error| format!("Could not start the update installer: {error}"))?;
    let deadline = Instant::now() + Duration::from_secs(20);
    loop {
        if ready.is_file() {
            return Ok(());
        }
        if let Some(status) = child.try_wait().map_err(|error| error.to_string())? {
            return Err(format!("Update installer exited before it was ready ({status}); see {}", stage.join("installer.log").display()));
        }
        if Instant::now() >= deadline {
            let _ = child.kill();
            let _ = child.wait();
            return Err("Update installer did not become ready; the application was not closed".into());
        }
        std::thread::sleep(Duration::from_millis(100));
    }
}

fn validate_stage(update_id: &str, executable: &Path) -> Result<PathBuf, String> {
    if update_id.len() != 32 || !update_id.bytes().all(|value| value.is_ascii_hexdigit()) {
        return Err("Invalid application update ID".into());
    }
    let directory = std::env::temp_dir().join(format!("galtransl-update-{update_id}"));
    let stage = directory.canonicalize().map_err(|error| error.to_string())?;
    if std::fs::symlink_metadata(&directory).map_err(|error| error.to_string())?.file_type().is_symlink() {
        return Err("Application update staging directory must not be a link".into());
    }
    let bytes = std::fs::read(stage.join("plan.json")).map_err(|error| error.to_string())?;
    let plan: UpdatePlan = serde_json::from_slice(&bytes).map_err(|error| format!("Invalid update plan: {error}"))?;
    let current = executable.canonicalize().map_err(|error| error.to_string())?;
    let root = current.parent().ok_or("Application executable has no directory")?;
    if plan.executable.canonicalize().map_err(|error| error.to_string())? != current
        || plan.install_dir.canonicalize().map_err(|error| error.to_string())? != root
        || plan.payload_dir.canonicalize().map_err(|error| error.to_string())? != stage.join("payload")
    {
        return Err("The update does not belong to this desktop installation".into());
    }
    Ok(stage)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn rejects_paths_in_place_of_an_update_id() {
        for value in ["", "../other", "C:/Windows", "fffffffffffffffffffffffffffffffz"] {
            assert!(validate_stage(value, Path::new("unused")).is_err());
        }
    }

    #[test]
    fn prepared_update_must_belong_to_the_current_installation() {
        let id = format!("{:032x}", std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).unwrap().as_nanos());
        let directory = std::env::temp_dir().join(format!("galtransl-update-{id}"));
        std::fs::create_dir(&directory).unwrap();
        std::fs::create_dir(directory.join("payload")).unwrap();
        let installation = directory.join("installation");
        std::fs::create_dir(&installation).unwrap();
        let executable = installation.join("desktop");
        std::fs::write(&executable, b"synthetic desktop").unwrap();
        let other = installation.join("other-desktop");
        std::fs::write(&other, b"other instance").unwrap();
        let plan = serde_json::json!({
            "install_dir": installation,
            "executable": executable,
            "payload_dir": directory.join("payload"),
        });
        std::fs::write(directory.join("plan.json"), serde_json::to_vec(&plan).unwrap()).unwrap();
        assert!(validate_stage(&id, &executable).is_ok());
        assert!(validate_stage(&id, &other).is_err());
        assert_eq!(directory.parent().unwrap(), std::env::temp_dir());
        std::fs::remove_dir_all(directory).unwrap();
    }
}

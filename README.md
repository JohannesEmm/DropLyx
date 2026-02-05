# DropLyx

**Real-time collaborative editing for LyX files via Dropbox**

**Author:** Johannes Emmerling (assisted by Claude)

DropLyx is a lightweight system tray application that enables collaborative editing of LyX documents through Dropbox. It provides file locking, conflict detection, and automatic merging of changes when multiple users edit the same document.

<img src="DropLyx_logo.png" alt="DropLyx Logo" width="50%">

## Features

- **File Locking**: Prevents conflicts by locking files while you're editing them
- **Real-time Monitoring**: Automatically detects when you open/close LyX files
- **Smart Auto-Merge**: Automatically merges non-conflicting changes from multiple editors
- **Visual Status Indicators**:
  - Light Blue: Monitoring, no files open
  - Green: You're editing files (all good)
  - Red: Someone else is editing a file
- **Desktop Notifications**: Get notified when files are locked/unlocked or when remote changes occur
- **Recursive Folder Monitoring**: Watches entire folder trees including subfolders
- **Conflict Resolution**: Creates backup files when automatic merging isn't possible

## How It Works

1. **File Detection**: DropLyx monitors LyX processes and detects open files by parsing window titles
2. **Lock Creation**: When you open a file, DropLyx creates a `.lock` file and a `.baseline` copy
3. **Change Detection**: Uses SHA256 hashing to detect when files are modified remotely (via Dropbox sync)
4. **3-Way Merge**: When you close a file with remote changes, it performs a Git-style 3-way merge:
   - Baseline: Original file when you started editing
   - Local: Your changes
   - Remote: Changes from other users
5. **Auto-Merge**: If changes don't conflict, they're automatically merged. Otherwise, backup files are created for manual resolution.

## Installation

### Option 1: Download Pre-built Binary

**Windows:** Download `DropLyx.exe` from the [Releases](https://github.com/yourusername/droplyx/releases) page.

**Linux:** Download the Linux binary or AppImage from the [Releases](https://github.com/yourusername/droplyx/releases) page.

**macOS:** Download `DropLyx.app` from the [Releases](https://github.com/yourusername/droplyx/releases) page. On first launch, you may need to right-click and select "Open" to bypass Gatekeeper.

### Option 2: Build from Source

**Windows:**

**Requirements:**
- Python 3.8+
- Dependencies listed in `requirements.txt`

**Steps:**

1. Clone the repository:
   ```bash
   git clone https://github.com/yourusername/droplyx.git
   cd droplyx
   ```

2. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```

3. Run directly:
   ```bash
   python DropLyx.py
   ```

4. Or build executable:
   ```bash
   pyinstaller DropLyx.spec
   ```
   The executable will be in the `dist` folder.

**Linux:**

See [BUILD_LINUX.md](BUILD_LINUX.md) for detailed Linux compilation instructions including system dependencies and AppImage creation.

**macOS:**

See [BUILD_MACOS.md](BUILD_MACOS.md) for detailed macOS compilation instructions including .app bundle creation and notarization.

## Usage

1. **First Launch**:
   - Run DropLyx.exe
   - Select your Dropbox folder (or any folder containing .lyx files)
   - The app will minimize to the system tray

2. **Monitor Status**:
   - Light blue icon: Ready, monitoring for LyX files
   - Green icon: You're editing a file
   - Red icon: Someone else has a file locked

3. **Work as Normal**:
   - Open and edit LyX files as usual
   - DropLyx automatically creates locks
   - If someone else edited the file, you'll get a notification

4. **Auto-Merge**:
   - When you close a file, DropLyx automatically merges remote changes
   - If conflicts exist, backup files are created:
     - `.remote_backup`: The other user's version
     - `.local_backup`: Your version
     - `.pre_merge_backup`: File state before merge attempt

5. **System Tray Menu**:
   - Right-click the icon for options
   - Add/remove watched folders
   - Check status
   - Quit the application

## Configuration

Settings are stored in `~/.lyx_sync_config.json`:

```json
{
  "watch_dirs": [
    "C:\\Users\\YourName\\Dropbox\\LyX"
  ]
}
```

## Technical Details

### File Lock Format
Lock files contain the username of the person editing:
```
filename.lyx.lock
```

### Baseline Tracking
Baseline files are created when editing starts:
```
filename.lyx.baseline
```

### Merge Algorithm
- Line-by-line comparison of baseline, local, and remote versions
- Changes that don't overlap are automatically merged
- Conflicting changes (same line edited differently) trigger manual resolution

### Conflict Detection
A conflict occurs when:
1. Baseline ≠ Local (you changed the line)
2. Baseline ≠ Remote (they changed the line)
3. Local ≠ Remote (changes differ)

## Limitations

- **Platform-Specific File Detection**:
  - Windows: Uses pygetwindow for window title parsing
  - Linux: Uses /proc filesystem and open file descriptors
- **LyX Specific**: Designed specifically for LyX files
- **Dropbox Sync**: Relies on Dropbox (or similar sync service) to sync files between machines
- **Line-based Merging**: Merge is line-based, not semantic (works well for LaTeX/LyX structure)

## Troubleshooting

**File detection not working:**
- Ensure LyX window titles contain the filename and folder path
- Check that watched folder includes the file location

**Merge conflicts:**
- Review backup files (`.local_backup`, `.remote_backup`)
- Manually resolve differences
- Copy resolved version over main file

**Lock files not removed:**
- Close DropLyx properly (right-click > Quit)
- Or manually delete `.lock` files

## Dependencies

- `Pillow`: Icon generation
- `pystray`: System tray integration
- `psutil`: Process monitoring
- `plyer`: Desktop notifications
- `pygetwindow`: Window title parsing (Windows)

## Contributing

Contributions are welcome! Please:

1. Fork the repository
2. Create a feature branch
3. Make your changes
4. Submit a pull request

## License

MIT License - See LICENSE file for details

## Acknowledgments

- LyX logo courtesy of the LyX project
- Inspired by collaborative editing needs in academic research
- Co-developed with Claude (Anthropic AI assistant) for performance optimization and multi-platform support

## Support

For issues, questions, or feature requests, please open an issue on GitHub.

---

**Note**: DropLyx is a community project and is not officially affiliated with LyX or Dropbox.

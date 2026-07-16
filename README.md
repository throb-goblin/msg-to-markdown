# MSG to Markdown

MSG to Markdown is a Windows desktop utility for converting Microsoft Outlook
`.msg` files into UTF-8 Markdown using Microsoft's `markitdown` package. It can
run as a Tkinter desktop app, process batches from the command line, or watch a
folder continuously for new email archives.

## Features

- Convert one or more selected `.msg` files, or watch a folder continuously.
- Drag and drop `.msg` files into Convert Once mode.
- Optionally recurse through subfolders.
- Preserve the input directory structure beneath the output folder.
- Name Markdown files from the email received time in `HHMMSS_DD-MM-YYYY`
  format, followed by the email subject.
- Prefer the MSG plain-text body where available to reduce HTML noise.
- Clean common email disclaimer/footer text while preserving sender name, role
  and the reply thread.
- Remove Outlook safelink wrappers while keeping the readable target URL.
- For emails with visible attachments, create an AI-friendly bundle folder with
  `README_index.md`, `main_email.md`, converted embedded emails and native
  attachment files together.
- Keep PDFs, DOCX, XLSX, CSV and other native attachments as native files.
- Convert embedded `.msg` attachments to Markdown and extract their visible
  attachments into the same bundle folder.
- Continue a batch when individual files fail.
- Keep the GUI responsive by running conversion and watch work on background
  threads.
- Choose System, Light or Windows-style Dark GUI theme.
- Apply the resolved theme to the Windows title bar where supported.
- Start the watcher automatically at Windows login.
- Minimise to the Windows notification area with `pystray`.
- Persist user settings in a local `config.json`.
- Write timestamped logs to `logs/`.

## Requirements

- Windows 10 or Windows 11.
- Python 3.12 or newer.
- PowerShell.
- Optional: .NET Framework C# compiler if you want to rebuild the lightweight
  Windows launcher with `build_launcher.ps1`.

Installable Python dependencies are listed in `requirements.txt`:

```text
markitdown[outlook]>=0.1.6
watchdog>=6.0.0
pystray>=0.19.5
Pillow>=12.3.0
winotify>=1.1.0
tkinterdnd2>=0.4.3
```

The `markitdown[outlook]` extra is intentional. Outlook `.msg` support is an
optional MarkItDown component, and it installs the `olefile` dependency used to
read MSG metadata, body text and attachments.

## Installation

Clone the repository and open PowerShell in the project folder:

```powershell
git clone <repo-url>
cd msg-to-markdown
```

Create and activate a virtual environment:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

If your `py` launcher points to a newer Python version, that is fine as long as
it is Python 3.12 or newer.

## Running the Desktop App

Start the GUI:

```powershell
.\.venv\Scripts\python.exe app.py
```

The first launch creates local `input_folder`, `output_folder`, `logs` and
`config.json` paths beside the app. These are intentionally ignored by Git.

To launch without keeping a PowerShell window open, rebuild or use the optional
launcher:

```powershell
.\build_launcher.ps1
.\MSG to Markdown.exe
```

`Launch MSG to Markdown.vbs` is also available as a script-based fallback. Both
launcher options start `app.py` through `.venv\Scripts\pythonw.exe`, so they use
the same installed virtual environment and avoid a visible console window.

## GUI Controls

- `Select file(s)`: shown in Convert Once mode; choose one or more `.msg`
  files, or drag `.msg` files onto the field.
- `Input Folder`: shown in Watch Folder mode; folder to monitor.
- `Output Folder`: destination folder for Markdown files.
- `Convert Once | Watch Folder`: select the active workflow.
- `Convert`: run a batch conversion in Convert Once mode.
- `Cancel`: shown only while a conversion is active.
- `Start Watching`: shown in Watch Folder mode while the watcher is stopped.
- `Stop Watching`: shown in Watch Folder mode while the watcher is active.
- `Open Output`: open the configured output folder in File Explorer.
- `Show Preferences` / `Hide Preferences`: expand or collapse the Live Log and
  Settings area.
- `Process subfolders`: recurse through nested folders.
- `Open output folder when finished`: open File Explorer after successful
  batches. Enabled by default.
- `Start watching when the application opens`: start watch mode when the app
  opens.
- `Close to notification area`: keep work running in the notification area when
  the window is closed or minimised.
- `GUI theme`: available inside `Preferences` > `Settings`; choose
  `System (default)`, `Light` or `Dark`.
- `Launch the application when I sign in`: available inside `Preferences` >
  `Settings`; starts the app minimised and begins watching after sign-in.
- `Show Windows desktop notifications`: available inside `Preferences` >
  `Settings`; disabled by default to avoid native shell popup issues on some
  managed Windows environments.

## Command Line Usage

Launch the GUI:

```powershell
.\.venv\Scripts\python.exe app.py
```

Convert one file:

```powershell
.\.venv\Scripts\python.exe app.py --input "C:\Path\To\Email.msg" --output "output_folder"
```

Convert a folder to a specific output folder:

```powershell
.\.venv\Scripts\python.exe app.py --input "input_folder" --output "output_folder"
```

Recurse through subfolders:

```powershell
.\.venv\Scripts\python.exe app.py --input "input_folder" --output "output_folder" --recursive
```

Overwrite existing Markdown files:

```powershell
.\.venv\Scripts\python.exe app.py --input "input_folder" --output "output_folder" --force
```

Watch continuously until you press `Ctrl+C`:

```powershell
.\.venv\Scripts\python.exe app.py --input "input_folder" --output "output_folder" --recursive --watch
```

Start the GUI minimised and begin watching immediately:

```powershell
.\.venv\Scripts\python.exe app.py --gui --start-minimized --watch-on-launch
```

## Output Structure

If the input tree is:

```text
input_folder/
|-- Client A/
|   |-- Advice.msg
|   `-- Emails/
|       |-- Email 1.msg
|       `-- Email 2.msg
```

The output tree is:

```text
output_folder/
`-- Client A/
    |-- Advice.md
    `-- Emails/
        |-- Email 1.md
        `-- 094633_14-07-2026 - Email 2/
            |-- README_index.md
            |-- main_email.md
            |-- embedded_email.md
            |-- advice.pdf
            |-- workbook.xlsx
            `-- notes.docx
```

Emails without visible attachments are written as a single `.md` file. Emails
with visible attachments are written as a folder named from the received time and
subject.

Each Markdown email file begins with front matter:

```yaml
---
document_type: "email"
source_file: "Advice.msg"
source_path: "Client A/Advice.msg"
title: "Optional subject when MarkItDown exposes one"
---
```

For bundle folders, `README_index.md` gives a recommended read order and full
file inventory. `main_email.md` lists visible attachments under
`## Attachments`. Hidden inline signature images are ignored. Embedded email
attachments are converted to linked Markdown files in the same bundle folder,
and visible attachments inside those embedded emails are extracted into that
same folder.

## Watch Folder Mode

Watch mode uses `watchdog` to detect `.msg` files created or moved into the
configured input folder. It ignores temporary files, waits for file size to
stabilise, then converts the email using the same conversion engine as the GUI
and CLI.

When Watch Folder starts or resumes from pause, it also scans the input folder
for existing `.msg` files and queues only those whose Markdown output is not
already present in the output folder.

The `Run watcher at Windows login` setting registers a per-user startup command
under:

```text
HKCU\Software\Microsoft\Windows\CurrentVersion\Run
```

It uses `pythonw.exe` when available so the watcher starts without opening a
console window.

## Configuration And Logs

User settings are stored locally in `config.json`. The file records the input
folder, output folder, window geometry, selected mode, checkbox choices,
Preferences area state, GUI theme, Windows login startup preference and
notification preference.

Every launch creates a timestamped log file in `logs/`. Logs include application
startup and shutdown, files processed or skipped, conversion failures, watch
folder events and batch summary totals.

Both `config.json` and `logs/` are ignored by Git.

## Data Safety

This project intentionally does not include sample `.msg` files, converted
Markdown, extracted attachments, logs, local configuration, virtual
environments, or generated launcher binaries. Keep real email archives and
converted outputs outside version control.

The `.gitignore` blocks common email/archive outputs, including `.msg`, `.eml`,
PDF, DOCX, XLSX, CSV and generated output folders, to reduce the chance of
publishing private material by accident.

## Rebuilding The Launcher

The optional `MSG to Markdown.exe` launcher is generated from
`LaunchMsgToMarkdown.cs`:

```powershell
.\build_launcher.ps1
```

The launcher is not required to run the app. It simply starts `app.py` with
`pythonw.exe` so the GUI can open without a console window. Generated `.exe`
files are ignored by Git.

## Extending The Parser

MarkItDown is isolated in `MarkItDownEmailBackend` inside `converter.py`.
Replacing it with another `.msg` parser, such as `extract-msg`, should only
require implementing the same `convert(source_path) -> tuple[str, str | None]`
method and injecting that backend into `MsgToMarkdownConverter`.

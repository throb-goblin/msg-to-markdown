using System;
using System.Diagnostics;
using System.IO;
using System.Linq;
using System.Text;
using System.Windows.Forms;

internal static class LaunchMsgToMarkdown
{
    [STAThread]
    private static int Main(string[] args)
    {
        string appDirectory = AppDomain.CurrentDomain.BaseDirectory;
        string pythonw = Path.Combine(appDirectory, ".venv", "Scripts", "pythonw.exe");
        string app = Path.Combine(appDirectory, "app.py");

        if (!File.Exists(pythonw))
        {
            MessageBox.Show(
                "Could not find .venv\\Scripts\\pythonw.exe beside the launcher.",
                "MSG to Markdown",
                MessageBoxButtons.OK,
                MessageBoxIcon.Error);
            return 1;
        }

        if (!File.Exists(app))
        {
            MessageBox.Show(
                "Could not find app.py beside the launcher.",
                "MSG to Markdown",
                MessageBoxButtons.OK,
                MessageBoxIcon.Error);
            return 1;
        }

        string arguments = Quote(app);
        if (args.Length > 0)
        {
            arguments += " " + string.Join(" ", args.Select(Quote));
        }

        ProcessStartInfo startInfo = new ProcessStartInfo
        {
            FileName = pythonw,
            Arguments = arguments,
            WorkingDirectory = appDirectory,
            UseShellExecute = false,
            CreateNoWindow = true
        };

        Process.Start(startInfo);
        return 0;
    }

    private static string Quote(string value)
    {
        if (value.Length == 0)
        {
            return "\"\"";
        }

        StringBuilder quoted = new StringBuilder("\"");
        int backslashCount = 0;

        foreach (char character in value)
        {
            if (character == '\\')
            {
                backslashCount++;
                continue;
            }

            if (character == '"')
            {
                quoted.Append('\\', backslashCount * 2 + 1);
                quoted.Append(character);
                backslashCount = 0;
                continue;
            }

            quoted.Append('\\', backslashCount);
            backslashCount = 0;
            quoted.Append(character);
        }

        quoted.Append('\\', backslashCount * 2);
        quoted.Append('"');
        return quoted.ToString();
    }
}

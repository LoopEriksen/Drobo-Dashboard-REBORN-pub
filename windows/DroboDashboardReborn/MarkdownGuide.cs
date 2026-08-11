using System;
using System.Collections.Generic;
using System.IO;
using System.Text;
using System.Text.RegularExpressions;

namespace DroboDashboardReborn;

/// <summary>
/// Converts docs/accessing-your-files.md into a small styled HTML page so
/// "Open the guide" shows something readable instead of a wall of raw `#`
/// and `**` in Notepad (the shell's default handler for .md on this
/// machine).
///
/// This is deliberately NOT a general Markdown engine -- it supports exactly
/// the handful of constructs the guide actually uses: headings, bold/italic,
/// inline code, fenced code blocks, a pipe table, ordered/unordered lists,
/// horizontal rules, links, and plain paragraphs. Anything outside that
/// subset would just fall through as a paragraph.
/// </summary>
internal static class MarkdownGuide
{
    private static readonly Regex HeadingRegex = new(@"^(#{1,6})\s+(.+?)\s*$", RegexOptions.Compiled);
    private static readonly Regex OrderedItemRegex = new(@"^\s*\d+\.\s+(.+)$", RegexOptions.Compiled);
    private static readonly Regex UnorderedItemRegex = new(@"^\s*[-*]\s+(.+)$", RegexOptions.Compiled);
    private static readonly Regex HrRegex = new(@"^-{3,}$", RegexOptions.Compiled);

    private static readonly Regex CodeSpanRegex = new(@"`([^`]+)`", RegexOptions.Compiled);
    private static readonly Regex BoldRegex = new(@"\*\*(.+?)\*\*", RegexOptions.Compiled);
    private static readonly Regex ItalicRegex = new(@"(?<!\*)\*(?!\*)([^*]+?)\*(?!\*)", RegexOptions.Compiled);
    private static readonly Regex LinkRegex = new(@"\[([^\]]+)\]\(([^)]+)\)", RegexOptions.Compiled);

    /// <summary>Reads the markdown file at <paramref name="markdownPath"/>, converts
    /// it, and writes a standalone HTML file to the user's temp directory (never the
    /// repo, never next to the exe -- an installed copy can sit under Program Files,
    /// which is read-only for a normal user). Returns the path to open.</summary>
    public static string ConvertFileToHtml(string markdownPath)
    {
        var markdown = File.ReadAllText(markdownPath);
        var html = ToHtml(markdown, Path.GetFileNameWithoutExtension(markdownPath));

        var outDir = Path.Combine(Path.GetTempPath(), "DroboDashboardReborn");
        Directory.CreateDirectory(outDir);
        var outPath = Path.Combine(outDir, Path.GetFileNameWithoutExtension(markdownPath) + ".html");
        File.WriteAllText(outPath, html);
        return outPath;
    }

    private static string ToHtml(string markdown, string fallbackTitle)
    {
        var lines = markdown.Replace("\r\n", "\n").Split('\n');
        var body = new StringBuilder();
        string? title = null;
        int i = 0;

        while (i < lines.Length)
        {
            var line = lines[i];

            if (string.IsNullOrWhiteSpace(line)) { i++; continue; }

            // fenced code block -- content is escaped verbatim, never run through
            // inline formatting, so a line like "RequireSecuritySignature : True <-- this
            // one" can't be mistaken for the start of an HTML tag.
            if (line.TrimStart().StartsWith("```"))
            {
                i++;
                var codeLines = new List<string>();
                while (i < lines.Length && !lines[i].TrimStart().StartsWith("```"))
                {
                    codeLines.Add(lines[i]);
                    i++;
                }
                if (i < lines.Length) i++; // skip the closing fence
                body.Append("<pre><code>").Append(Escape(string.Join("\n", codeLines))).Append("</code></pre>\n");
                continue;
            }

            // heading
            var heading = HeadingRegex.Match(line);
            if (heading.Success)
            {
                int level = heading.Groups[1].Value.Length;
                var text = heading.Groups[2].Value;
                title ??= Escape(text.Replace("**", "").Replace("`", ""));
                body.Append($"<h{level}>").Append(FormatInline(text)).Append($"</h{level}>\n");
                i++;
                continue;
            }

            // horizontal rule
            if (HrRegex.IsMatch(line.Trim()))
            {
                body.Append("<hr>\n");
                i++;
                continue;
            }

            // pipe table: a "| cell | cell |" row immediately followed by a
            // "|---|---|" separator row
            if (line.TrimStart().StartsWith("|") && i + 1 < lines.Length && IsTableSeparatorRow(lines[i + 1]))
            {
                var headerCells = SplitRow(line);
                i += 2; // header + separator
                var rows = new List<string[]>();
                while (i < lines.Length && lines[i].TrimStart().StartsWith("|"))
                {
                    rows.Add(SplitRow(lines[i]));
                    i++;
                }

                body.Append("<table>\n<thead><tr>");
                foreach (var c in headerCells) body.Append("<th>").Append(FormatInline(c)).Append("</th>");
                body.Append("</tr></thead>\n<tbody>\n");
                foreach (var row in rows)
                {
                    body.Append("<tr>");
                    foreach (var c in row) body.Append("<td>").Append(FormatInline(c)).Append("</td>");
                    body.Append("</tr>\n");
                }
                body.Append("</tbody>\n</table>\n");
                continue;
            }

            // ordered / unordered list
            var orderedStart = OrderedItemRegex.Match(line);
            var unorderedStart = orderedStart.Success ? Match.Empty : UnorderedItemRegex.Match(line);
            if (orderedStart.Success || unorderedStart.Success)
            {
                bool ordered = orderedStart.Success;
                var marker = ordered ? OrderedItemRegex : UnorderedItemRegex;
                var items = new List<string>();
                var current = new StringBuilder((ordered ? orderedStart : unorderedStart).Groups[1].Value);
                i++;

                while (i < lines.Length)
                {
                    var l = lines[i];

                    if (string.IsNullOrWhiteSpace(l))
                    {
                        // A blank line only ends the list if what follows isn't another
                        // item of the same kind -- the guide has a couple of "loose"
                        // lists with a blank line between items.
                        int j = i + 1;
                        while (j < lines.Length && string.IsNullOrWhiteSpace(lines[j])) j++;
                        if (j < lines.Length && marker.IsMatch(lines[j])) { i = j; continue; }
                        break;
                    }

                    var m = marker.Match(l);
                    if (m.Success)
                    {
                        items.Add(current.ToString());
                        current = new StringBuilder(m.Groups[1].Value);
                        i++;
                        continue;
                    }

                    // A new block starting with no blank line ends the list. Doesn't
                    // happen anywhere in today's guide (every block is blank-line
                    // separated) but costs nothing to guard against.
                    if (HeadingRegex.IsMatch(l) || l.TrimStart().StartsWith("```")
                        || HrRegex.IsMatch(l.Trim()) || l.TrimStart().StartsWith("|"))
                        break;

                    current.Append(' ').Append(l.Trim()); // soft-wrapped continuation line
                    i++;
                }
                items.Add(current.ToString());

                var tag = ordered ? "ol" : "ul";
                body.Append('<').Append(tag).Append(">\n");
                foreach (var it in items) body.Append("<li>").Append(FormatInline(it)).Append("</li>\n");
                body.Append("</").Append(tag).Append(">\n");
                continue;
            }

            // paragraph: consume soft-wrapped lines until the next blank line or block
            var paraLines = new List<string> { line.Trim() };
            i++;
            while (i < lines.Length && !string.IsNullOrWhiteSpace(lines[i])
                   && !lines[i].TrimStart().StartsWith("```")
                   && !HeadingRegex.IsMatch(lines[i])
                   && !HrRegex.IsMatch(lines[i].Trim())
                   && !OrderedItemRegex.IsMatch(lines[i])
                   && !UnorderedItemRegex.IsMatch(lines[i])
                   && !lines[i].TrimStart().StartsWith("|"))
            {
                paraLines.Add(lines[i].Trim());
                i++;
            }
            body.Append("<p>").Append(FormatInline(string.Join(" ", paraLines))).Append("</p>\n");
        }

        return WrapPage(title ?? Escape(fallbackTitle), body.ToString());
    }

    /// <summary>A table separator row is only "-", ":" and "|" characters, with at
    /// least one dash per cell -- e.g. "|---|---|" or "| :--- | ---: |".</summary>
    private static bool IsTableSeparatorRow(string line)
    {
        var trimmed = line.Trim();
        if (!trimmed.Contains('|')) return false;
        var cells = trimmed.Trim('|').Split('|');
        if (cells.Length == 0) return false;
        foreach (var cell in cells)
        {
            var c = cell.Trim();
            if (c.Length == 0 || !c.Contains('-')) return false;
            foreach (var ch in c)
                if (ch != '-' && ch != ':') return false;
        }
        return true;
    }

    private static string[] SplitRow(string line)
    {
        var trimmed = line.Trim().Trim('|');
        var cells = trimmed.Split('|');
        for (int i = 0; i < cells.Length; i++) cells[i] = cells[i].Trim();
        return cells;
    }

    /// <summary>Escapes HTML-significant characters, then applies inline markdown
    /// (links, code spans, bold, italic) on top of the already-escaped text -- so a
    /// path like "\\10.0.0.5\Share" or a code span containing "&lt;drobo&gt;" comes
    /// through as visible text instead of being parsed as a tag.</summary>
    private static string FormatInline(string raw)
    {
        var escaped = Escape(raw);
        escaped = LinkRegex.Replace(escaped, m =>
            $"<a href=\"{m.Groups[2].Value}\" target=\"_blank\" rel=\"noopener\">{m.Groups[1].Value}</a>");
        escaped = CodeSpanRegex.Replace(escaped, m => $"<code>{m.Groups[1].Value}</code>");
        escaped = BoldRegex.Replace(escaped, m => $"<strong>{m.Groups[1].Value}</strong>");
        escaped = ItalicRegex.Replace(escaped, m => $"<em>{m.Groups[1].Value}</em>");
        return escaped;
    }

    private static string Escape(string s) => s
        .Replace("&", "&amp;")
        .Replace("<", "&lt;")
        .Replace(">", "&gt;")
        .Replace("\"", "&quot;");

    /// <summary>Styling matches the app itself -- same dark background and greys/blues
    /// as MainWindow.xaml's Window.Resources -- so the guide reads as part of the
    /// product rather than a document that escaped it.</summary>
    private static string WrapPage(string title, string bodyHtml) => $@"<!doctype html>
<html lang=""en"">
<head>
<meta charset=""utf-8"">
<meta name=""viewport"" content=""width=device-width, initial-scale=1"">
<title>{title}</title>
<style>
  html {{ color-scheme: dark; }}
  body {{
    background: #0D1116;
    color: #E6EBF0;
    font-family: ""Segoe UI"", system-ui, -apple-system, ""Helvetica Neue"", Arial, sans-serif;
    font-size: 16px;
    line-height: 1.6;
    margin: 0;
    padding: 40px 20px 80px;
  }}
  .doc {{ max-width: 70ch; margin: 0 auto; }}
  h1, h2, h3, h4, h5, h6 {{ color: #E6EBF0; font-weight: 600; line-height: 1.3; }}
  h1 {{ font-size: 1.6em; border-bottom: 1px solid #262E37; padding-bottom: 0.4em; }}
  h2 {{ font-size: 1.3em; margin-top: 1.8em; }}
  h3 {{ font-size: 1.1em; margin-top: 1.5em; color: #8FB4DC; }}
  p, li {{ color: #C6CDD6; }}
  a {{ color: #5AA6E0; }}
  a:hover {{ text-decoration: none; }}
  strong {{ color: #E6EBF0; }}
  em {{ color: #C6CDD6; }}
  code {{
    font-family: Consolas, ""Cascadia Mono"", ui-monospace, monospace;
    background: #161B21;
    border: 1px solid #262E37;
    border-radius: 3px;
    padding: 0.1em 0.4em;
    font-size: 0.9em;
    color: #E9A83C;
  }}
  pre {{
    background: #161B21;
    border: 1px solid #262E37;
    border-radius: 4px;
    padding: 12px 14px;
    overflow-x: auto;
  }}
  pre code {{ background: none; border: none; padding: 0; color: #C6CDD6; }}
  hr {{ border: none; border-top: 1px solid #262E37; margin: 2em 0; }}
  table {{ border-collapse: collapse; width: 100%; margin: 1em 0; font-size: 0.95em; }}
  th, td {{ border: 1px solid #262E37; padding: 6px 10px; text-align: left; vertical-align: top; }}
  th {{ background: #161B21; color: #8B93A1; font-family: Consolas, monospace; font-size: 0.85em; text-transform: uppercase; }}
  ul, ol {{ padding-left: 1.4em; }}
  li {{ margin-bottom: 0.3em; }}
</style>
</head>
<body>
<div class=""doc"">
{bodyHtml}</div>
</body>
</html>
";
}

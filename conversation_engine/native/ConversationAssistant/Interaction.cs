using System.Diagnostics;

namespace ConversationAssistant;

public record Box(double Left, double Top, double Right, double Bottom)
{
    public double Width => Right - Left;
    public double Height => Bottom - Top;
    public bool Positive => Width > 0 && Height > 0;
    public double[] Array => [Left, Top, Right, Bottom];
}
public record WindowKey(long Hwnd, int Pid);
public record WindowInfo(WindowKey Key, string Process, Box Rect, bool Visible = true, bool Minimized = false);
public record Source(string Id, string Name, string Kind = "");
public record Tracking(string Status, string? SourceId = null, string? Name = null,
    string[]? Candidates = null, double[]? WindowRect = null, string Reason = "");
public record ActionResult(bool Ok, string? Error = null, bool Invoked = false,
    string? Message = null);
public sealed class InteractionFailure(string message) : Exception(message);

// Native providers and synthetic fixtures implement the same narrow interface.
public interface INode
{
    string Kind { get; }
    string Id { get; }
    string Class { get; }
    string Name { get; }
    string RuntimeId { get; }
    int Pid { get; }
    bool Offscreen { get; }
    bool Enabled { get; }
    Box Rect { get; }
    IEnumerable<INode> Children();
    string ReadValue();
    bool CanSetValue { get; }
    void SetValue(string text);
    void Focus();
    bool CanInvoke { get; }
    void Invoke();
}
public interface IWindows
{
    WindowInfo? Foreground();
    WindowInfo? Inspect(long hwnd);
    INode Root(long hwnd);
    INode HeaderRoot(long hwnd) => Root(hwnd);
    string? FocusedId();
    bool Activate(WindowInfo window);
    double IdleSeconds();
    bool Paste(string text, bool replace, Func<bool> guard);
}

public static class Selectors
{
    public const string VerifiedTitle = "content_view.top_content_view.title_h_view.left_v_view.left_content_v_view.left_ui_.big_title_line_h_view.current_chat_name_label";
    public static readonly HashSet<string> Titles = new(StringComparer.OrdinalIgnoreCase)
        { "chattitle", "chat_title", "conversationtitle", "conversation_title", VerifiedTitle };
    static readonly HashSet<string> Headers = new(StringComparer.OrdinalIgnoreCase)
        { "chatheader", "chat_header", "conversationheader", "conversation_header" };
    static readonly HashSet<string> HeaderClasses = new(StringComparer.OrdinalIgnoreCase)
        { "chatheader", "chatheaderwidget", "conversationheader", "conversationheaderwidget" };
    static readonly HashSet<string> Skip = ["Edit", "Document", "List", "ListItem", "DataGrid", "Table", "Tree", "TreeItem"];
    static readonly HashSet<string> Containers = ["Window", "Pane", "Group", "Custom", "Header", "HeaderItem", "ToolBar"];

    public static bool OnTitlePath(string id) => id.Length > 0 &&
        (id.Equals(VerifiedTitle, StringComparison.OrdinalIgnoreCase) || VerifiedTitle.StartsWith(id + ".", StringComparison.OrdinalIgnoreCase));

    // The conversation sidebar has a fixed width, not a fraction of a maximized
    // window. The verified title path supplies identity; geometry only bounds it
    // to the header. Generic capability selectors keep their stricter fallback.
    public static bool HeaderRegion(Box r, Box w, bool strict, bool verified = false)
    {
        double left = verified ? w.Left : w.Left + w.Width * .25;
        return r.Positive && w.Positive && (strict
            ? r.Left >= left && r.Right <= w.Right && r.Top >= w.Top && r.Bottom <= w.Top + Math.Min(w.Height * .25, 240)
            : r.Right > left && r.Left < w.Right && r.Bottom > w.Top && r.Top < w.Top + Math.Min(w.Height * .25, 240));
    }

    public static List<string> HeaderNames(INode root, Box window)
    {
        var queue = new Queue<(INode Node, int Depth, bool Header)>();
        queue.Enqueue((root, 0, false));
        var names = new List<string>();
        int visited = 0;
        while (queue.Count > 0 && visited++ < 250)
        {
            var (node, depth, insideHeader) = queue.Dequeue();
            if (Skip.Contains(node.Kind) || node.Offscreen) continue;
            if (node.Pid != root.Pid) continue;
            bool verified = OnTitlePath(node.Id);
            if (depth > 0 && !HeaderRegion(node.Rect, window, false, verified)) continue;
            bool safe = HeaderRegion(node.Rect, window, true, verified);
            bool header = safe && (node.Kind == "Header" || Headers.Contains(node.Id) || HeaderClasses.Contains(node.Class));
            bool title = safe && Titles.Contains(node.Id);
            if (title || header || (insideHeader && safe && node.Kind is "Text" or "HeaderItem"))
            {
                var name = node.Name.Trim();
                if (name.Length is > 0 and <= 256) names.Add(name);
            }
            bool ancestor = node.Id.Length > 0 && Titles.Any(t => t.StartsWith(node.Id + ".", StringComparison.OrdinalIgnoreCase));
            if (depth < 32 && (depth == 0 || Containers.Contains(node.Kind) || header || ancestor))
            {
                foreach (var child in node.Children())
                {
                    queue.Enqueue((child, depth + 1, insideHeader || header));
                    if (queue.Count + visited > 250) throw new InteractionFailure("会话标题结构超出读取范围，请手动选择。");
                }
            }
        }
        if (queue.Count > 0) throw new InteractionFailure("会话标题结构超出读取范围，请手动选择。");
        return names.Distinct().ToList();
    }

    public static Tracking Match(IEnumerable<string> names, IEnumerable<Source> sources)
    {
        var candidates = names.Select(n => n.Trim()).Where(n => n.Length > 0).ToHashSet(StringComparer.Ordinal);
        if (candidates.Count == 0)
            return new("title_unavailable", Reason: "未读取到当前会话标题，请让微信聊天窗口可见后重试。");
        var found = sources.Where(s => s.Id.Length > 0 && (candidates.Contains(s.Name) ||
            s.Kind is "群聊" or "group" or "chatroom" && candidates.Any(n =>
                System.Text.RegularExpressions.Regex.Replace(n, @"\s*[（(]\d+[）)]$", "") == s.Name)))
            .GroupBy(s => s.Id).Select(g => g.First()).ToArray();
        return found.Length switch
        {
            0 => new("unmatched", Reason: "当前会话未能精确匹配，请手动选择。"),
            1 => new("matched", found[0].Id, found[0].Name, [found[0].Id], Reason: "会话名称已精确匹配。"),
            _ => new("ambiguous", Candidates: found.Select(s => s.Id).Order().ToArray(), Reason: "名称对应多个会话，请手动选择。")
        };
    }

    static INode Unique(INode root, Func<INode, bool> predicate, string error)
    {
        var queue = new Queue<(INode Node, int Depth)>();
        queue.Enqueue((root, 0));
        INode? found = null;
        int visited = 0;
        while (queue.Count > 0 && visited++ < 400)
        {
            var (node, depth) = queue.Dequeue();
            if (node.Offscreen) continue;
            if (predicate(node))
            {
                if (found != null) throw new InteractionFailure(error);
                found = node;
            }
            if (Skip.Contains(node.Kind) || (depth > 0 && node.Kind == "Window")) continue;
            if (depth < 36 && (depth == 0 || Containers.Contains(node.Kind)))
                foreach (var child in node.Children())
                {
                    queue.Enqueue((child, depth + 1));
                    if (queue.Count + visited > 400) throw new InteractionFailure(error);
                }
        }
        return queue.Count == 0 && found != null ? found : throw new InteractionFailure(error);
    }

    public static INode Composer(INode root, WindowInfo w) => Unique(root, n =>
        n.Kind == "Edit" && n.Id == "chat_input_field" && n.Enabled && n.Pid == w.Key.Pid && n.Rect.Positive &&
        n.Rect.Left >= w.Rect.Left && n.Rect.Right <= w.Rect.Right &&
        n.Rect.Top >= w.Rect.Top + w.Rect.Height * .4 && n.Rect.Bottom <= w.Rect.Bottom,
        "未找到唯一可编辑的微信输入框，草稿已保留。");

    public static INode Send(INode root, WindowInfo w, INode editor) => Unique(root, n =>
        n.Kind == "Button" && n.Pid == w.Key.Pid && n.Name is "发送" or "发送(S)" or "发送（S）" or "Send" or "Send (S)" &&
        n.Rect.Positive && n.Rect.Left >= editor.Rect.Left && n.Rect.Top >= editor.Rect.Top &&
        n.Rect.Right <= w.Rect.Right && n.Rect.Bottom <= w.Rect.Bottom,
        "没有找到唯一发送控件，请你接管。");
}

// The production provider implements only these narrow reads. Offline fixtures
// exercise this locator without constructing UIA or opening any windows.
public interface ITitleAccess
{
    INode? At(double x, double y);
    INode? Parent(INode node);
    IReadOnlyList<INode> PathChildren(INode node, string[] exactIds);
}

public static class TitleLocator
{
    public const int MaxQueries = 20;
    static readonly string[] PathIds = Selectors.VerifiedTitle.Split('.')
        .Select((_, i) => string.Join(".", Selectors.VerifiedTitle.Split('.').Take(i + 1))).ToArray();

    public static INode? Find(INode root, WindowInfo window, ITitleAccess access, double dpi, Box? previous = null)
    {
        int queries = 0;
        var visited = new HashSet<string>(StringComparer.Ordinal);
        void Spend()
        {
            if (++queries > MaxQueries) throw new InteractionFailure("会话标题读取达到范围上限，请稍后重试。");
        }
        bool Usable(INode n) => n.Pid == window.Key.Pid && !n.Offscreen &&
            n.Kind is not ("Edit" or "Document" or "List" or "ListItem" or "DataGrid" or "Table" or "Tree" or "TreeItem");
        INode? Path(INode start, bool isRoot = false)
        {
            INode node = start;
            while (Usable(node))
            {
                if (!isRoot && !Selectors.HeaderRegion(node.Rect, window.Rect, false, Selectors.OnTitlePath(node.Id))) return null;
                if (Selectors.Titles.Contains(node.Id))
                    return Selectors.HeaderRegion(node.Rect, window.Rect, true, Selectors.OnTitlePath(node.Id)) ? node : null;
                if (!isRoot && !Selectors.OnTitlePath(node.Id)) return null;
                if (!visited.Add(node.RuntimeId)) return null;
                string[] ids = isRoot ? [..PathIds, ..Selectors.Titles] : PathIds.Where(id =>
                    id.StartsWith(node.Id + ".", StringComparison.OrdinalIgnoreCase)).ToArray();
                Spend();
                var children = access.PathChildren(node, ids);
                // Never pick one of two candidate paths or accept a provider's
                // unrelated child. No generic container/message walk is allowed.
                if (children.Count > 1) throw new InteractionFailure("会话标题存在多个候选，请手动选择。");
                if (children.Count == 0 || !ids.Contains(children[0].Id, StringComparer.OrdinalIgnoreCase)) return null;
                node = children[0]; isRoot = false;
            }
            return null;
        }
        INode? Point(double x, double y)
        {
            Spend();
            var node = access.At(x, y);
            for (int depth = 0; node != null && Usable(node); depth++)
            {
                var title = Path(node);
                if (title != null) return title;
                if (depth == 2) break;
                Spend(); node = access.Parent(node);
            }
            return null;
        }
        // A prior rectangle is only a hint. Re-read and verify ID, PID and header
        // geometry on every sample; no stale name or binding is reused here.
        if (previous != null && Selectors.HeaderRegion(previous, window.Rect, true, true))
        {
            var cached = Point((previous.Left + previous.Right) / 2, (previous.Top + previous.Bottom) / 2);
            if (cached != null) return cached;
        }
        var direct = Path(root, true);
        if (direct != null) return direct;
        foreach (double fraction in new[] { .42, .55, .32 })
        {
            var title = Point(window.Rect.Left + window.Rect.Width * fraction, window.Rect.Top + 48 * dpi);
            if (title != null) return title;
        }
        return null;
    }
}

public sealed class Interaction(IWindows windows, int assistantPid, Func<double>? clock = null)
{
    readonly Func<double> clock = clock ?? (() => Stopwatch.GetTimestamp() / (double)Stopwatch.Frequency);
    readonly Dictionary<(WindowKey, string, string), string> placed = [];
    WindowKey? stableWindow;
    string? stableSource;
    double stableSince;
    public WindowKey? LastWeChat { get; private set; }
    public static string Normalize(string s) => s.Replace("\r\n", "\n").Replace('\r', '\n');
    public static bool Usable(WindowInfo? w) => w != null && w.Visible && !w.Minimized && w.Rect.Positive &&
        (w.Process.Equals("weixin.exe", StringComparison.OrdinalIgnoreCase) || w.Process.Equals("wechat.exe", StringComparison.OrdinalIgnoreCase));

    Tracking Failure(string status, string reason)
    {
        stableWindow = null; stableSource = null;
        return new(status, Reason: reason);
    }

    public Tracking Sample(Source[] sources)
    {
        try
        {
            var fg = windows.Foreground();
            WindowInfo? w;
            if (Usable(fg)) { w = fg; LastWeChat = fg!.Key; }
            else if (fg?.Key.Pid == assistantPid && LastWeChat != null)
            {
                w = windows.Inspect(LastWeChat.Hwnd);
                if (w?.Key != LastWeChat) w = null;
            }
            else return Failure("wechat_unavailable", "请先打开需要分析的微信会话。");
            if (!Usable(w)) return Failure("wechat_unavailable", "微信窗口不可见、已最小化或已关闭。");
            var root = windows.HeaderRoot(w!.Key.Hwnd);
            if (root.Pid != w.Key.Pid) return Failure("unsupported", "微信窗口已变化。");
            var names = Selectors.HeaderNames(root, w.Rect);
            var latest = windows.Foreground();
            if (latest?.Key != w.Key && latest?.Key.Pid != assistantPid)
                return Failure("wechat_unavailable", "读取期间窗口发生切换。");
            var result = Selectors.Match(names, sources) with { WindowRect = w.Rect.Array };
            if (result.Status != "matched") { stableWindow = null; stableSource = null; return result; }
            double now = clock();
            if (stableWindow != w.Key || stableSource != result.SourceId || now < stableSince)
            {
                stableWindow = w.Key; stableSource = result.SourceId; stableSince = now;
            }
            return now - stableSince + 1e-9 >= .6 ? result : result with
            { Status = "unmatched", SourceId = null, Name = null, Reason = "正在确认会话切换，请稍候。" };
        }
        catch { return Failure("unsupported", "会话识别暂不可用，请手动选择。"); }
    }

    public bool Available(WindowKey? key)
    {
        try { var fg = windows.Foreground(); return key != null && fg?.Key == key && Usable(fg) && windows.IdleSeconds() >= 1.5; }
        catch { return false; }
    }

    (WindowInfo Window, INode Root, INode Editor, string Value) Checked(WindowKey key, string sid, Source[] sources, Func<bool> guard, bool foreground)
    {
        var w = windows.Inspect(key.Hwnd);
        var fg = windows.Foreground();
        if (!Usable(w) || w!.Key != key || !guard() ||
            (fg?.Key != key && (foreground || fg?.Key.Pid != assistantPid)))
            throw new InteractionFailure("会话、焦点或草稿已变化，请重新操作。");
        var root = windows.Root(key.Hwnd);
        if (root.Pid != key.Pid || !root.Enabled) throw new InteractionFailure("微信当前有弹窗或不可编辑，草稿已保留。");
        var match = Selectors.Match(Selectors.HeaderNames(root, w.Rect), sources);
        if (match.Status != "matched" || match.SourceId != sid) throw new InteractionFailure("微信会话与草稿不一致或存在同名联系人。");
        var editor = Selectors.Composer(root, w);
        return (w, root, editor, Normalize(editor.ReadValue()));
    }

    public ActionResult Place(string text, string sid, Source[] sources, WindowKey? key, Func<bool> guard)
    {
        if (string.IsNullOrWhiteSpace(text) || text.Length > 20000 || text.Any(c => c < 32 && c is not '\r' and not '\n' and not '\t'))
            return new(false, "草稿为空、过长或含无效字符。");
        try
        {
            if (key == null) throw new InteractionFailure("请先打开对应的微信会话。");
            var initial = Checked(key, sid, sources, guard, false);
            var editorKey = (key, sid, initial.Editor.RuntimeId);
            var desired = Normalize(text);
            placed.TryGetValue(editorKey, out var previous);
            if (initial.Value.Length > 0 && initial.Value != desired && initial.Value != previous)
                throw new InteractionFailure("微信输入框已有你写的内容，已保留。清空后再点生成建议。");
            if (!windows.Activate(initial.Window)) throw new InteractionFailure("暂时无法聚焦微信输入框，草稿已保留。");
            initial.Editor.Focus();
            bool PasteGuard()
            {
                var current = Checked(key, sid, sources, guard, true);
                return current.Editor.RuntimeId == editorKey.Item3 && windows.FocusedId() == editorKey.Item3 && current.Value == initial.Value;
            }
            if (!PasteGuard()) throw new InteractionFailure("输入框或会话刚刚变化，未覆盖原内容。");
            if (initial.Value != desired)
            {
                var current = Checked(key, sid, sources, guard, true);
                var editor = current.Editor;
                if (editor.RuntimeId != editorKey.Item3 || current.Value != initial.Value || windows.FocusedId() != editorKey.Item3)
                    throw new InteractionFailure("输入框或会话刚刚变化，未覆盖原内容。");
                if (editor.CanSetValue)
                {
                    // A provider may apply a value and then fail. Read back before
                    // deciding whether the guarded paste fallback is safe.
                    try { editor.SetValue(text); } catch { }
                }
                var value = Normalize(editor.ReadValue());
                if (value != desired)
                {
                    if (value != initial.Value) throw new InteractionFailure("输入框文字已变化，请检查草稿。");
                    if (!windows.Paste(text, initial.Value.Length > 0, PasteGuard))
                        throw new InteractionFailure("未能填入微信，草稿已保留，可点击复制。");
                    for (int i = 0; i < 7; i++)
                    {
                        var readback = Checked(key, sid, sources, guard, true);
                        if (readback.Editor.RuntimeId != editorKey.Item3) throw new InteractionFailure("微信输入框已变化。");
                        if (readback.Value == desired) break;
                        if (i < 6) Thread.Sleep(80);
                    }
                }
            }
            var final = Checked(key, sid, sources, guard, true);
            if (final.Editor.RuntimeId != editorKey.Item3 || final.Value != desired)
                throw new InteractionFailure("微信未确认草稿填入，请检查输入框。");
            if (placed.Count > 256) placed.Clear();
            placed[editorKey] = desired;
            return new(true, Message: "已放入微信输入框，请修改后自行发送。");
        }
        catch (InteractionFailure e) { return new(false, e.Message); }
        catch { return new(false, "暂未能可靠填入微信；草稿已保留，可点击复制。"); }
    }

    public ActionResult Send(string text, string sid, Source[] sources, WindowKey? key, Func<bool> guard, Func<bool> record)
    {
        bool invoked = false;
        try
        {
            if (key == null || !Available(key)) throw new InteractionFailure("请保持微信在前台，并暂停手工输入。");
            var source = sources.SingleOrDefault(s => s.Id == sid);
            if (source?.Kind is not ("私聊" or "private" or "contact" or "friend"))
                throw new InteractionFailure("自动聊天仅支持明确启用的私聊。");
            (INode Editor, INode Button) Check(string expected)
            {
                var c = Checked(key, sid, sources, guard, true);
                if (c.Value != Normalize(expected)) throw new InteractionFailure("微信输入框已被修改，请你接管。");
                return (c.Editor, Selectors.Send(c.Root, c.Window, c.Editor));
            }
            var initial = Check("");
            string editorId = initial.Editor.RuntimeId, buttonId = initial.Button.RuntimeId;
            if (!initial.Button.CanInvoke) throw new InteractionFailure("当前发送控件不支持自动调用。");
            var result = Place(text, sid, sources, key, guard);
            if (!result.Ok) return result;
            var pair = initial;
            for (int i = 0; i < 7; i++)
            {
                pair = Check(text);
                if (pair.Editor.RuntimeId != editorId || pair.Button.RuntimeId != buttonId)
                    throw new InteractionFailure("发送控件或输入框已变化，请你接管。");
                if (pair.Button.Enabled) break;
                if (i < 6) Thread.Sleep(80);
            }
            if (!pair.Button.Enabled || !record()) throw new InteractionFailure("发送前核对或记录失败，未调用发送。");
            pair = Check(text);
            if (pair.Editor.RuntimeId != editorId || pair.Button.RuntimeId != buttonId || !pair.Button.Enabled || !pair.Button.CanInvoke)
                throw new InteractionFailure("发送前控件发生变化，请你接管。");
            invoked = true;
            pair.Button.Invoke();
            return new(true, Invoked: true);
        }
        catch (InteractionFailure e) { return new(false, e.Message, invoked); }
        catch { return new(false, "发送结果无法确认，请检查微信；不会自动重试。", invoked); }
    }
}

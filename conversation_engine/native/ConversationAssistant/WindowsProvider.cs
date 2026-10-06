using System.Runtime.InteropServices;
using System.Text;
using System.Windows;
using System.Windows.Threading;
using FlaUI.Core.AutomationElements;
using FlaUI.UIA3;
using FlaUI.Core;
using FlaUI.Core.Definitions;
using FlaUI.Core.Conditions;

namespace ConversationAssistant;

public sealed class WindowsProvider : IWindows, IDisposable
{
    readonly UIA3Automation automation;
    readonly Dispatcher dispatcher;
    readonly bool targeted;
    WindowKey? titleWindow;
    Box? titleWindowRect, titleRect;
    public WindowsProvider(Dispatcher dispatcher, bool targeted = false)
    {
        this.dispatcher = dispatcher;
        this.targeted = targeted;
        automation = new UIA3Automation
        {
            ConnectionTimeout = TimeSpan.FromMilliseconds(800),
            TransactionTimeout = TimeSpan.FromMilliseconds(1200)
        };
    }
    public WindowInfo? Foreground() => Inspect(NativeMethods.GetForegroundWindow().ToInt64());
    public WindowInfo? Inspect(long hwnd)
    {
        var handle = new IntPtr(hwnd);
        if (hwnd == 0 || !NativeMethods.IsWindow(handle)) return null;
        NativeMethods.GetWindowThreadProcessId(handle, out uint pid);
        var process = NativeMethods.OpenProcess(0x1000, false, pid);
        if (process == IntPtr.Zero) return null;
        try
        {
            var path = new StringBuilder(32768);
            uint capacity = (uint)path.Capacity;
            if (!NativeMethods.QueryFullProcessImageName(process, 0, path, ref capacity)) return null;
            if (NativeMethods.DwmGetWindowAttribute(handle, 9, out var r, Marshal.SizeOf<NativeMethods.Rect>()) != 0) return null;
            return new(new(hwnd, checked((int)pid)), System.IO.Path.GetFileName(path.ToString()),
                new(r.Left, r.Top, r.Right, r.Bottom), NativeMethods.IsWindowVisible(handle), NativeMethods.IsIconic(handle));
        }
        finally { NativeMethods.CloseHandle(process); }
    }
    public INode Root(long hwnd) => targeted ? PointRoot(hwnd, true) : new UiaNode(automation.FromHandle(new IntPtr(hwnd)));
    public INode HeaderRoot(long hwnd) => targeted ? PointRoot(hwnd, false) : Root(hwnd);
    // Read exact ID paths through immediate children, never a descendant scan.
    // Points are only fallback hints. All reads share one bounded query policy
    // and cached metadata; ID, geometry and PID still decide the binding.
    INode PointRoot(long hwnd, bool input)
    {
        var w = Inspect(hwnd) ?? throw new InteractionFailure("微信窗口已变化。");
        double dpi = Math.Max(96, NativeMethods.GetDpiForWindow(new IntPtr(hwnd))) / 96.0;
        var cache = new CacheRequest { TreeScope = TreeScope.Element, AutomationElementMode = AutomationElementMode.Full };
        var p = automation.PropertyLibrary.Element;
        foreach (var id in new[] { p.ControlType, p.AutomationId, p.ClassName, p.Name, p.RuntimeId,
            p.ProcessId, p.IsOffscreen, p.IsEnabled, p.BoundingRectangle }) cache.Add(id);
        using var activated = cache.Activate();
        var root = automation.FromHandle(new IntPtr(hwnd));
        var nodes = new List<INode>();
        UiaNode At(double x, double y, Func<UiaNode, bool> matches)
        {
            using (cache.Activate())
            {
                var element = automation.FromPoint(new((int)x, (int)y));
                var node = new UiaNode(element, true);
                for (int depth = 0; depth < 2 && node.Pid == w.Key.Pid && !matches(node); depth++)
                {
                    var parent = automation.TreeWalkerFactory.GetControlViewWalker().GetParent(element);
                    if (parent == null) break;
                    element = parent; node = new UiaNode(element, true);
                }
                return node;
            }
        }
        var previous = titleWindow == w.Key && titleWindowRect == w.Rect ? titleRect : null;
        var title = TitleLocator.Find(new UiaNode(root, true), w, new UiaTitleAccess(automation), dpi, previous);
        titleWindow = w.Key; titleWindowRect = w.Rect; titleRect = title?.Rect;
        if (title != null) nodes.Add(title);
        if (input && nodes.Count == 1)
        {
            var editor = At(w.Rect.Left + w.Rect.Width * .65, w.Rect.Bottom - 96 * dpi, n => n.Id == "chat_input_field");
            if (editor.Pid == w.Key.Pid && editor.Id == "chat_input_field") nodes.Add(editor);
            var send = At(w.Rect.Right - 42 * dpi, w.Rect.Bottom - 28 * dpi, n => n.Kind == "Button");
            if (send.Pid == w.Key.Pid && send.Kind == "Button") nodes.Add(send);
        }
        return new UiaNode(root, true, nodes);
    }
    public string? FocusedId() => new UiaNode(automation.FocusedElement()).RuntimeId;
    public bool Activate(WindowInfo w) => NativeMethods.SetForegroundWindow(new IntPtr(w.Key.Hwnd)) && Foreground()?.Key == w.Key;
    public double IdleSeconds()
    {
        var input = new NativeMethods.LastInput { Size = (uint)Marshal.SizeOf<NativeMethods.LastInput>() };
        return NativeMethods.GetLastInputInfo(ref input) ? unchecked(NativeMethods.GetTickCount() - input.Tick) / 1000.0 : 0;
    }
    public bool Paste(string text, bool replace, Func<bool> guard)
    {
        if (!guard()) return false;
        dispatcher.Invoke(() => Clipboard.SetText(text));
        bool Chord(ushort key)
        {
            if (!guard() || new[] { 16, 17, 18, 91, 92 }.Any(k => (NativeMethods.GetAsyncKeyState(k) & 0x8000) != 0)) return false;
            NativeMethods.Input[] events = [NativeMethods.Key(17), NativeMethods.Key(key), NativeMethods.Key(key, true), NativeMethods.Key(17, true)];
            if (NativeMethods.SendInput(4, events, Marshal.SizeOf<NativeMethods.Input>()) == 4) return true;
            NativeMethods.Input[] releases = [NativeMethods.Key(key, true), NativeMethods.Key(17, true)];
            NativeMethods.SendInput(2, releases, Marshal.SizeOf<NativeMethods.Input>());
            return false;
        }
        return (!replace || Chord(0x41)) && Chord(0x56);
    }
    public void Dispose() => automation.Dispose();
}

sealed class UiaTitleAccess(UIA3Automation automation) : ITitleAccess
{
    public INode? At(double x, double y) => new UiaNode(automation.FromPoint(new((int)x, (int)y)), true);
    public INode? Parent(INode node)
    {
        var parent = automation.TreeWalkerFactory.GetControlViewWalker().GetParent(((UiaNode)node).Element);
        return parent == null ? null : new UiaNode(parent, true);
    }
    public IReadOnlyList<INode> PathChildren(INode node, string[] exactIds)
    {
        if (exactIds.Length == 0) return Array.Empty<INode>();
        var condition = new OrCondition(exactIds.Distinct().Select(id => automation.ConditionFactory.ByAutomationId(id)));
        return ((UiaNode)node).Element.FindAllChildren(condition).Select(e => (INode)new UiaNode(e, true)).ToArray();
    }
}

public sealed class UiaNode : INode
{
    readonly AutomationElement element;
    internal AutomationElement Element => element;
    readonly UiaSnapshot? snapshot;
    readonly IEnumerable<INode>? children;
    public UiaNode(AutomationElement element, bool cached = false, IEnumerable<INode>? children = null)
    {
        this.element = element;
        if (cached)
        {
            snapshot = new(Kind, Id, Class, Name, RuntimeId, Pid, Offscreen, Enabled, Rect);
            this.children = children ?? Array.Empty<INode>();
        }
    }
    record UiaSnapshot(string Kind, string Id, string Class, string Name, string RuntimeId, int Pid, bool Offscreen, bool Enabled, Box Rect);
    public string Kind => snapshot?.Kind ?? element.ControlType.ToString();
    public string Id => snapshot?.Id ?? element.AutomationId ?? "";
    public string Class => snapshot?.Class ?? element.ClassName ?? "";
    public string Name => snapshot?.Name ?? element.Name ?? "";
    public string RuntimeId => snapshot?.RuntimeId ?? string.Join(",", element.Properties.RuntimeId.Value);
    public int Pid => snapshot?.Pid ?? element.Properties.ProcessId.Value;
    public bool Offscreen => snapshot?.Offscreen ?? element.IsOffscreen;
    public bool Enabled => snapshot?.Enabled ?? element.IsEnabled;
    public Box Rect { get { if (snapshot != null) return snapshot.Rect; var r = element.BoundingRectangle; return new(r.Left, r.Top, r.Right, r.Bottom); } }
    public IEnumerable<INode> Children() => children ?? element.FindAllChildren().Select(e => new UiaNode(e));
    public string ReadValue()
    {
        if (element.Patterns.Value.TryGetPattern(out var pattern))
        {
            if (pattern.IsReadOnly.Value) throw new InteractionFailure("微信输入框当前只读，草稿已保留。");
            return pattern.Value.Value;
        }
        var values = new List<string>();
        if (element.Patterns.LegacyIAccessible.TryGetPattern(out var legacy)) values.Add(legacy.Value.Value);
        if (element.Patterns.Text.TryGetPattern(out var text)) values.Add(text.DocumentRange.GetText(20001));
        if (values.Count > 0 && values.Select(Interaction.Normalize).Distinct().Count() == 1) return values[0];
        throw new InteractionFailure("无法一致地核对微信输入框文字，草稿已保留。");
    }
    public bool CanSetValue => element.Patterns.Value.TryGetPattern(out var p) && !p.IsReadOnly.Value;
    public void SetValue(string text) => element.Patterns.Value.Pattern.SetValue(text);
    public void Focus() => element.FrameworkAutomationElement.SetFocus();
    public bool CanInvoke => element.Patterns.Invoke.IsSupported;
    public void Invoke() => element.Patterns.Invoke.Pattern.Invoke();
}

internal static class NativeMethods
{
    [StructLayout(LayoutKind.Sequential)] internal struct Rect { public int Left, Top, Right, Bottom; }
    [StructLayout(LayoutKind.Sequential)] internal struct LastInput { public uint Size, Tick; }
    [StructLayout(LayoutKind.Sequential)] internal struct KeyboardInput { public ushort Key, Scan; public uint Flags, Time; public UIntPtr Extra; }
    [StructLayout(LayoutKind.Sequential)] internal struct MouseInput { public int X, Y; public uint Data, Flags, Time; public UIntPtr Extra; }
    [StructLayout(LayoutKind.Explicit)] internal struct InputUnion { [FieldOffset(0)] public KeyboardInput Keyboard; [FieldOffset(0)] public MouseInput Mouse; }
    [StructLayout(LayoutKind.Sequential)] internal struct Input { public uint Type; public InputUnion Data; }
    internal static Input Key(ushort key, bool up = false) => new() { Type = 1, Data = new() { Keyboard = new() { Key = key, Flags = up ? 2u : 0 } } };
    [DllImport("user32.dll")] internal static extern IntPtr GetForegroundWindow();
    [DllImport("user32.dll")] internal static extern uint GetDpiForWindow(IntPtr hwnd);
    [DllImport("user32.dll")] [return: MarshalAs(UnmanagedType.Bool)] internal static extern bool IsWindow(IntPtr hwnd);
    [DllImport("user32.dll")] [return: MarshalAs(UnmanagedType.Bool)] internal static extern bool IsWindowVisible(IntPtr hwnd);
    [DllImport("user32.dll")] [return: MarshalAs(UnmanagedType.Bool)] internal static extern bool IsIconic(IntPtr hwnd);
    [DllImport("user32.dll")] internal static extern uint GetWindowThreadProcessId(IntPtr hwnd, out uint pid);
    [DllImport("kernel32.dll")] internal static extern IntPtr OpenProcess(uint access, bool inherit, uint pid);
    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, EntryPoint = "QueryFullProcessImageNameW")]
    [return: MarshalAs(UnmanagedType.Bool)] internal static extern bool QueryFullProcessImageName(IntPtr handle, uint flags, StringBuilder path, ref uint size);
    [DllImport("kernel32.dll")] [return: MarshalAs(UnmanagedType.Bool)] internal static extern bool CloseHandle(IntPtr handle);
    [DllImport("dwmapi.dll")] internal static extern int DwmGetWindowAttribute(IntPtr hwnd, uint attribute, out Rect rect, int size);
    [DllImport("user32.dll")] [return: MarshalAs(UnmanagedType.Bool)] internal static extern bool SetForegroundWindow(IntPtr hwnd);
    [DllImport("user32.dll")] [return: MarshalAs(UnmanagedType.Bool)] internal static extern bool GetLastInputInfo(ref LastInput input);
    [DllImport("kernel32.dll")] internal static extern uint GetTickCount();
    [DllImport("user32.dll")] internal static extern short GetAsyncKeyState(int key);
    [DllImport("user32.dll")] internal static extern uint SendInput(uint count, Input[] inputs, int size);
    [DllImport("user32.dll")] internal static extern bool ReleaseCapture();
    [DllImport("user32.dll")] internal static extern IntPtr SendMessage(IntPtr hwnd, uint msg, IntPtr wParam, IntPtr lParam);
}

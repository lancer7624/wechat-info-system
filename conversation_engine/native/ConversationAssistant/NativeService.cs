using System.Diagnostics;
using System.IO;
using System.Text;
using System.Text.Json;
using System.Windows.Threading;

namespace ConversationAssistant;

public static class NativeService
{
    public static int Run(int parent)
    {
        var dispatcher = Dispatcher.CurrentDispatcher;
        var thread = new Thread(() =>
        {
            try { Serve(parent, dispatcher); }
            catch { /* No chat-bearing diagnostics on stdout/stderr. */ }
            finally { dispatcher.BeginInvokeShutdown(DispatcherPriority.Send); }
        }) { IsBackground = true };
        thread.SetApartmentState(ApartmentState.MTA); thread.Start();
        Dispatcher.Run(); return 0;
    }

    static void Serve(int parent, Dispatcher dispatcher)
    {
        using var input = new StreamReader(Console.OpenStandardInput(), new UTF8Encoding(false));
        using var output = new StreamWriter(Console.OpenStandardOutput(), new UTF8Encoding(false)) { AutoFlush = true };
        using var provider = new WindowsProvider(dispatcher, targeted: true);
        var engine = new Interaction(provider, parent);
        long sequence = 0;
        void Write(object value) => output.WriteLine(JsonSerializer.Serialize(value, WorkerClient.Json));
        JsonElement Read()
        {
            var value = new StringBuilder();
            for (int c; (c = input.Read()) >= 0;)
            {
                if (c == '\n') { using var doc = JsonDocument.Parse(value.ToString()); return doc.RootElement.Clone(); }
                if (value.Length >= WorkerClient.MaxFrame) throw new IOException();
                value.Append((char)c);
            }
            throw new EndOfStreamException();
        }
        bool Callback(string method, string token)
        {
            string id = "p:" + ++sequence;
            Write(new { id, method, args = new[] { token } });
            var answer = Read();
            return answer.GetProperty("id").GetString() == id && answer.GetProperty("result").ValueKind == JsonValueKind.True;
        }
        Source[] Sources(JsonElement value) => value.EnumerateArray().Select(s => new Source(
            s.GetProperty("id").GetString()!, s.GetProperty("name").GetString()!,
            s.TryGetProperty("kind", out var kind) ? kind.GetString() ?? "" : "")).ToArray();
        WindowKey? Key(JsonElement value) => value.ValueKind == JsonValueKind.Array && value.GetArrayLength() == 2 ? new(value[0].GetInt64(), value[1].GetInt32()) : null;
        string previousInput = "", previousSources = "";
        Tracking? cached = null;
        double sampledAt = 0;
        while (true)
        {
            var request = Read();
            string method = request.GetProperty("method").GetString()!;
            var args = request.GetProperty("args");
            object result;
            if (method == "native.sample")
            {
                var last = new NativeMethods.LastInput { Size = (uint)System.Runtime.InteropServices.Marshal.SizeOf<NativeMethods.LastInput>() };
                NativeMethods.GetLastInputInfo(ref last);
                var foreground = provider.Foreground();
                string stamp = $"{foreground?.Key}:{foreground?.Rect}:{last.Tick}";
                string sources = args[0].GetRawText();
                double now = Stopwatch.GetTimestamp() / (double)Stopwatch.Frequency;
                bool changed = stamp != previousInput || sources != previousSources;
                if (cached == null || changed || cached.Status == "unmatched" && now - sampledAt >= 1 ||
                    cached.Status == "title_unavailable" && now - sampledAt >= 3 || now - sampledAt >= 30)
                {
                    // At most one sample per second. A changed input invalidates
                    // the cached binding immediately, even during throttling.
                    if (now - sampledAt < 1 && cached != null)
                        cached = new("unmatched", Reason: "正在确认会话切换，请稍候。");
                    else
                    {
                        cached = engine.Sample(Sources(args[0]));
                        previousInput = stamp; previousSources = sources; sampledAt = now;
                    }
                }
                result = new { tracking = cached, window_key = engine.LastWeChat is { } w ? new long[] { w.Hwnd, w.Pid } : null };
            }
            else if (method == "native.available") result = engine.Available(Key(args[0]));
            else if (method is "native.place" or "native.send")
            {
                string token = args[4].GetString()!;
                Func<bool> guard = () => Callback("native_guard", token);
                result = method == "native.place" ? engine.Place(args[0].GetString()!, args[1].GetString()!, Sources(args[2]), Key(args[3]), guard)
                    : engine.Send(args[0].GetString()!, args[1].GetString()!, Sources(args[2]), Key(args[3]), guard, () => Callback("native_record", token));
                cached = null;
            }
            else throw new IOException();
            Write(new { id = request.GetProperty("id").GetString(), result });
        }
    }
}

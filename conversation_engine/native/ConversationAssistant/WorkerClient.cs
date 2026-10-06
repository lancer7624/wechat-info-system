using System.Collections.Concurrent;
using System.Diagnostics;
using System.IO;
using System.Text;
using System.Text.Json;

namespace ConversationAssistant;

public sealed class WorkerClient : IDisposable
{
    public const int MaxFrame = 16 * 1024 * 1024;
    public static readonly JsonSerializerOptions Json = new() { PropertyNamingPolicy = JsonNamingPolicy.SnakeCaseLower };
    readonly Process process;
    readonly ProcessJob job = new();
    readonly ConcurrentDictionary<string, TaskCompletionSource<JsonElement>> pending = new();
    readonly SemaphoreSlim writes = new(1);
    readonly CancellationTokenSource stopped = new();
    long sequence;
    public Func<string, JsonElement, Task<object?>>? NativeRequest { get; set; }

    public WorkerClient(string exe, IEnumerable<string> args)
    {
        var start = new ProcessStartInfo(exe)
        {
            UseShellExecute = false, CreateNoWindow = true,
            RedirectStandardInput = true, RedirectStandardOutput = true, RedirectStandardError = true,
            StandardInputEncoding = new UTF8Encoding(false), StandardOutputEncoding = new UTF8Encoding(false),
            StandardErrorEncoding = new UTF8Encoding(false), WorkingDirectory = Path.GetDirectoryName(exe)!
        };
        foreach (var arg in args) start.ArgumentList.Add(arg);
        start.Environment["PYTHONDONTWRITEBYTECODE"] = "1";
        start.Environment["PYTHONIOENCODING"] = "utf-8";
        process = Process.Start(start) ?? throw new IOException("Worker did not start");
        try { job.Assign(process); }
        catch { process.Kill(true); process.Dispose(); job.Dispose(); throw; }
        _ = DrainErrors();
        _ = ReadLoop();
    }

    async Task DrainErrors()
    {
        // Drain, never persist or display chat-bearing library tracebacks.
        char[] buffer = new char[4096];
        try { while (await process.StandardError.ReadAsync(buffer.AsMemory(), stopped.Token) != 0) { } }
        catch (Exception) when (stopped.IsCancellationRequested) { }
        catch (IOException) { }
    }
    async Task Write(object value)
    {
        string line = JsonSerializer.Serialize(value, Json);
        if (line.Length > MaxFrame) throw new IOException("Frame too large");
        await writes.WaitAsync(stopped.Token);
        try
        {
            await process.StandardInput.WriteLineAsync(line.AsMemory(), stopped.Token);
            await process.StandardInput.FlushAsync(stopped.Token);
        }
        finally { writes.Release(); }
    }
    public async Task<JsonElement> Call(string method, object?[] args, int timeoutSeconds = 20)
    {
        string id = "h:" + Interlocked.Increment(ref sequence);
        var source = new TaskCompletionSource<JsonElement>(TaskCreationOptions.RunContinuationsAsynchronously);
        if (stopped.IsCancellationRequested || !pending.TryAdd(id, source)) throw new IOException("Worker stopped");
        try
        {
            await Write(new { id, method, args });
            return await source.Task.WaitAsync(TimeSpan.FromSeconds(timeoutSeconds), stopped.Token);
        }
        finally { pending.TryRemove(id, out _); }
    }
    async Task Respond(string id, string method, JsonElement args)
    {
        object? result;
        try { result = NativeRequest == null ? null : await NativeRequest(method, args); }
        catch { result = new ActionResult(false, "窗口交互未完成，请重新操作。"); }
        try { await Write(new { id, result }); } catch { Stop(); }
    }
    async Task ReadLoop()
    {
        char[] buffer = new char[4096];
        var frame = new StringBuilder();
        try
        {
            while (!stopped.IsCancellationRequested)
            {
                int read = await process.StandardOutput.ReadAsync(buffer.AsMemory(), stopped.Token);
                if (read == 0) break;
                for (int i = 0; i < read; i++)
                {
                    if (buffer[i] != '\n')
                    {
                        if (frame.Length >= MaxFrame) throw new IOException("Frame too large");
                        frame.Append(buffer[i]); continue;
                    }
                    using var document = JsonDocument.Parse(frame.ToString());
                    frame.Clear();
                    var root = document.RootElement;
                    var id = root.GetProperty("id").GetString() ?? throw new IOException("Missing id");
                    if (root.TryGetProperty("method", out var name))
                    {
                        if (!id.StartsWith("p:", StringComparison.Ordinal) || id.Length > 14) throw new IOException("Invalid id");
                        _ = Respond(id, name.GetString()!, root.GetProperty("args").Clone());
                    }
                    else if (pending.TryRemove(id, out var source)) source.TrySetResult(root.GetProperty("result").Clone());
                }
            }
        }
        catch (Exception) { /* Surface only a generic disconnected status. */ }
        finally { Stop(); }
    }
    void Stop()
    {
        if (!stopped.IsCancellationRequested) stopped.Cancel();
        foreach (var entry in pending) entry.Value.TrySetException(new IOException("Worker disconnected"));
    }
    public void Dispose()
    {
        Stop();
        try { process.StandardInput.Close(); } catch { }
        // Killing the job also reaps ProcessPoolExecutor children on host crash.
        job.Dispose();
        process.Dispose();
    }
}

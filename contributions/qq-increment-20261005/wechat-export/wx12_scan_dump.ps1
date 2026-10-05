# 全内存 32B 熵窗口频次统计(离线 dump)
# 输出 wx12_scan_hits.json: {hex: {count, offset}}
$src = @'
using System;
using System.Collections.Generic;
using System.IO;

public static class DumpScan {
    static int[] pc8;
    static ulong[,] tb;

    public static void Run(string path, string outJson) {
        pc8 = new int[256];
        for (int v = 0; v < 256; v++) pc8[v] = (v & 1) + ((v >> 1) & 1) + ((v >> 2) & 1) + ((v >> 3) & 1)
            + ((v >> 4) & 1) + ((v >> 5) & 1) + ((v >> 6) & 1) + ((v >> 7) & 1);
        tb = new ulong[65536, 4];
        for (int v = 0; v < 65536; v++) {
            int lo = v & 0xFF, hi = v >> 8;
            tb[v, lo >> 6] |= 1UL << (lo & 63);
            tb[v, hi >> 6] |= 1UL << (hi & 63);
        }
        byte[] data = File.ReadAllBytes(path);
        int n = data.Length - 31;
        var counts = new Dictionary<ulong, long>();
        for (int off = 0; off < n; off += 16) {
            ulong a = 0, b = 0, c = 0, d = 0;
            for (int j = 0; j < 16; j++) {
                int t = data[off + 2 * j] | (data[off + 2 * j + 1] << 8);
                a |= tb[t, 0]; b |= tb[t, 1]; c |= tb[t, 2]; d |= tb[t, 3];
            }
            int dist = 0;
            for (int i = 0; i < 8; i++)
                dist += pc8[(byte)(a >> (8 * i))] + pc8[(byte)(b >> (8 * i))]
                      + pc8[(byte)(c >> (8 * i))] + pc8[(byte)(d >> (8 * i))];
            if (dist < 16) continue;
            ulong h = BitConverter.ToUInt64(data, off) ^ BitConverter.ToUInt64(data, off + 8)
                   ^ BitConverter.ToUInt64(data, off + 16) ^ BitConverter.ToUInt64(data, off + 24);
            long v;
            if (counts.TryGetValue(h, out v)) {
                int cnt = (int)(v >> 32);
                if (cnt < 0x7FFFFFF0) counts[h] = ((long)(cnt + 1) << 32) | (v & 0xFFFFFFFFL);
            } else {
                counts[h] = (1L << 32) | (long)(off & 0xFFFFFFFFL);
            }
        }
        var sb = new System.Text.StringBuilder();
        sb.Append("[");
        bool first = true;
        int written = 0;
        foreach (var kv in counts) {
            int cnt = (int)(kv.Value >> 32);
            if (cnt < 2) continue;
            long off = kv.Value & 0xFFFFFFFFL;
            if (!first) sb.Append(",");
            first = false;
            sb.Append("{\"h\":\"").Append(kv.Key.ToString("x16")).Append("\",\"c\":")
              .Append(cnt).Append(",\"o\":").Append(off).Append("}");
            written++;
            if (written >= 200000) break;
        }
        sb.Append("]");
        File.WriteAllText(outJson, sb.ToString());
        Console.WriteLine("窗口种数(频次>=2,上限20万): " + written);
    }
}
'@
Add-Type -TypeDefinition $src -Language CSharp
[DumpScan]::Run("$PSScriptRoot\wx12_full.dmp", "$PSScriptRoot\wx12_scan_hits.json")

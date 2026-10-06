using ConversationAssistant;

static class Program
{
    [STAThread]
    public static int Main(string[] args)
    {
        if (args.Length != 2 || args[0] != "--native-service" || !int.TryParse(args[1], out int parent) || parent <= 0) return 1;
        return NativeService.Run(parent);
    }
}

// CFX Maestro sidecar bridge.
//
// Bridges PyLabRobot (Python, any OS) to the CFX Maestro / CFX Manager WCF API.
// The WCF service uses WSDualHttpBinding with WS-SecureConversation (SPNEGO),
// WS-ReliableMessaging, and a duplex callback contract -- none of which are
// reachable from a non-.NET client. This sidecar owns that complexity and
// exposes one trivial HTTP endpoint:
//
//     POST /xmlcommand
//     Content-Type: application/xml
//     body: the <Message> XML document (built by PyLabRobot)
//
//     200 OK
//     body: the <Blocks> XML document (the XmlCommandResult string)
//
//     GET /health  ->  200 "ok"
//
// The sidecar is a dumb pass-through: it does not parse or interpret the XML.
// All protocol logic lives in pylabrobot.thermocycling.biorad.cfx_maestro.
//
// Usage:
//     CfxMaestroSidecar.exe \
//         --service-url http://localhost:8003/BioRad.PCR.CommandManager/SOCFXCommandService \
//         --listen http://+:8080/ \
//         --callback-base http://localhost:8081/cfx-callback
//
// All three are optional; defaults match a local CFX Maestro instance.

using System;
using System.IO;
using System.Net;
using System.ServiceModel;
using System.Text;
using System.Threading;

namespace CfxMaestroSidecar
{
  // ---- WCF service contract (hand-written from SOCFXCommandService.wsdl) ----
  //
  // Alternatively, generate this with svcutil against the live service (see
  // generate-proxy.cmd) and delete this block.

  [ServiceContract(
    Namespace = "http://BioRad.PCR.CommandManager",
    ConfigurationName = "ISOCFXCommandService",
    CallbackContract = typeof(ISOCFXCommandServiceCallback))]
  public interface ISOCFXCommandService
  {
    [OperationContract(
      Action = "http://BioRad.PCR.CommandManager/ISOCFXCommandService/XmlCommand",
      ReplyAction = "http://BioRad.PCR.CommandManager/ISOCFXCommandService/XmlCommandResponse")]
    string XmlCommand(string xml);

    [OperationContract(
      Action = "http://BioRad.PCR.CommandManager/ISOCFXCommandService/SubscribeToServiceIsClosing",
      ReplyAction = "http://BioRad.PCR.CommandManager/ISOCFXCommandService/SubscribeToServiceIsClosingResponse")]
    void SubscribeToServiceIsClosing();
  }

  // The service's duplex callback. Reserved for Bio-Rad internal use and not
  // supported by the API, but a stub instance context is required to satisfy
  // the duplex service contract (per the API Reference Guide).
  public interface ISOCFXCommandServiceCallback
  {
    [OperationContract(
      IsOneWay = true,
      Action = "http://BioRad.PCR.CommandManager/ISOCFXCommandService/OnServiceIsClosing")]
    void OnServiceIsClosing();
  }

  [CallbackBehavior(UseSynchronizationContext = false)]
  public sealed class CallbackStub : ISOCFXCommandServiceCallback
  {
    public void OnServiceIsClosing()
    {
      // Intentionally a no-op (reserved for Bio-Rad internal use).
    }
  }

  // Owns the WCF channel and keeps it healthy. WSDualHttpBinding uses a reliable
  // session with a duplex callback; the channel faults on session timeout or
  // after any error, and then every call throws. This manager lazily (re)creates
  // the channel when it is not in the Opened state, and retries a call once if it
  // faults mid-flight. Calls are serialized (the underlying duplex/reliable
  // session is not designed for concurrent callers).
  internal sealed class ClientManager
  {
    private readonly Options _opts;
    private readonly object _lock = new object();
    private DuplexChannelFactory<ISOCFXCommandService> _factory;
    private ISOCFXCommandService _client;

    public ClientManager(Options opts) => _opts = opts;

    public string XmlCommand(string messageXml)
    {
      lock (_lock)
      {
        try
        {
          return EnsureClient().XmlCommand(messageXml);
        }
        catch (Exception ex) when (IsChannelFault(ex))
        {
          Console.Error.WriteLine($"[sidecar] channel faulted ({ex.GetType().Name}); recreating and retrying once.");
          ResetClient();
          return EnsureClient().XmlCommand(messageXml);
        }
      }
    }

    private static bool IsChannelFault(Exception ex) =>
      ex is CommunicationException || ex is TimeoutException || ex is ObjectDisposedException;

    private ISOCFXCommandService EnsureClient()
    {
      var co = _client as ICommunicationObject;
      if (_client != null && co != null && co.State == CommunicationState.Opened)
      {
        return _client;
      }

      // Discard a non-healthy channel before making a new one.
      if (co != null)
      {
        try { co.Abort(); } catch { /* ignore */ }
        _client = null;
      }

      if (_factory == null || _factory.State == CommunicationState.Faulted)
      {
        if (_factory != null)
        {
          try { _factory.Abort(); } catch { /* ignore */ }
        }
        _factory = CreateFactory(_opts);
      }

      _client = _factory.CreateChannel();
      ((ICommunicationObject)_client).Open(); // surface handshake errors here
      return _client;
    }

    private void ResetClient()
    {
      var co = _client as ICommunicationObject;
      if (co != null)
      {
        try { co.Abort(); } catch { /* ignore */ }
      }
      _client = null;
    }

    private static DuplexChannelFactory<ISOCFXCommandService> CreateFactory(Options opts)
    {
      // WSDualHttpBinding defaults already match the published policy:
      //   security mode = Message, negotiated SecureConversation (SPNEGO),
      //   reliable sessions enabled, duplex over composite channels.
      // We only raise the size/quota limits (status XML can be large), per the
      // recommended app.config in the Bio-Rad Examples Kit.
      var binding = new WSDualHttpBinding
      {
        MaxReceivedMessageSize = 2147483647,
        ClientBaseAddress = new Uri(opts.CallbackBase),
        ReceiveTimeout = TimeSpan.FromMinutes(20),
        SendTimeout = TimeSpan.FromMinutes(2),
      };
      binding.ReaderQuotas.MaxStringContentLength = 2147483647;
      binding.ReaderQuotas.MaxArrayLength = 2147483647;
      binding.ReaderQuotas.MaxBytesPerRead = 2147483647;
      binding.ReaderQuotas.MaxDepth = 256;

      var address = new EndpointAddress(opts.ServiceUrl);
      return new DuplexChannelFactory<ISOCFXCommandService>(
        new InstanceContext(new CallbackStub()), binding, address);
    }
  }

  internal static class Program
  {
    private static int Main(string[] args)
    {
      var opts = Options.Parse(args);
      if (opts == null)
      {
        return 2; // usage already printed
      }

      Console.WriteLine($"[sidecar] service-url   = {opts.ServiceUrl}");
      Console.WriteLine($"[sidecar] listen        = {opts.ListenPrefix}");
      Console.WriteLine($"[sidecar] callback-base = {opts.CallbackBase}");

      var clients = new ClientManager(opts);

      using (var listener = new HttpListener())
      {
        listener.Prefixes.Add(opts.ListenPrefix);
        try
        {
          listener.Start();
        }
        catch (HttpListenerException ex)
        {
          Console.Error.WriteLine(
            $"[sidecar] cannot bind {opts.ListenPrefix}: {ex.Message}. " +
            "On Windows you may need to run as admin or reserve the URL with netsh.");
          return 1;
        }

        Console.WriteLine("[sidecar] ready. Ctrl+C to stop.");
        var stop = new ManualResetEventSlim(false);
        Console.CancelKeyPress += (_, e) => { e.Cancel = true; stop.Set(); };

        // Serve on a background thread so we can wait on Ctrl+C cleanly.
        var worker = new Thread(() => ServeLoop(listener, clients)) { IsBackground = true };
        worker.Start();

        stop.Wait();
        Console.WriteLine("[sidecar] shutting down.");
        listener.Stop();
      }

      return 0;
    }

    private static void ServeLoop(HttpListener listener, ClientManager clients)
    {
      while (listener.IsListening)
      {
        HttpListenerContext ctx;
        try
        {
          ctx = listener.GetContext();
        }
        catch (Exception)
        {
          return; // listener stopped
        }

        ThreadPool.QueueUserWorkItem(_ => Handle(ctx, clients));
      }
    }

    private static void Handle(HttpListenerContext ctx, ClientManager clients)
    {
      try
      {
        var path = ctx.Request.Url.AbsolutePath.TrimEnd('/');

        if (ctx.Request.HttpMethod == "GET" && path.EndsWith("/health"))
        {
          Write(ctx, 200, "text/plain", "ok");
          return;
        }

        if (ctx.Request.HttpMethod != "POST")
        {
          Write(ctx, 405, "text/plain", "method not allowed");
          return;
        }

        string messageXml;
        using (var reader = new StreamReader(ctx.Request.InputStream, Encoding.UTF8))
        {
          messageXml = reader.ReadToEnd();
        }

        if (string.IsNullOrWhiteSpace(messageXml))
        {
          Write(ctx, 400, "text/plain", "empty request body");
          return;
        }

        // The one and only WCF call. ClientManager recreates the channel if it
        // has faulted (WSDualHttpBinding reliable sessions fault on timeout or
        // after a prior error), and retries once.
        string blocksXml = clients.XmlCommand(messageXml);
        Write(ctx, 200, "application/xml", blocksXml ?? string.Empty);
      }
      catch (Exception ex)
      {
        Console.Error.WriteLine($"[sidecar] request error: {ex}");
        try { Write(ctx, 502, "text/plain", "CFX Maestro call failed: " + ex.Message); }
        catch { /* client gone */ }
      }
    }

    private static void Write(HttpListenerContext ctx, int status, string contentType, string body)
    {
      var bytes = Encoding.UTF8.GetBytes(body);
      ctx.Response.StatusCode = status;
      ctx.Response.ContentType = contentType;
      ctx.Response.ContentLength64 = bytes.Length;
      ctx.Response.OutputStream.Write(bytes, 0, bytes.Length);
      ctx.Response.OutputStream.Close();
    }
  }

  internal sealed class Options
  {
    public string ServiceUrl = "http://localhost:8003/BioRad.PCR.CommandManager/SOCFXCommandService";
    public string ListenPrefix = "http://+:8080/";
    public string CallbackBase = "http://localhost:8081/cfx-callback";

    public static Options Parse(string[] args)
    {
      var o = new Options();
      for (int i = 0; i < args.Length; i++)
      {
        switch (args[i])
        {
          case "--service-url": o.ServiceUrl = args[++i]; break;
          case "--listen": o.ListenPrefix = args[++i]; break;
          case "--callback-base": o.CallbackBase = args[++i]; break;
          case "-h":
          case "--help":
            PrintUsage();
            return null;
          default:
            Console.Error.WriteLine($"unknown argument: {args[i]}");
            PrintUsage();
            return null;
        }
      }
      return o;
    }

    private static void PrintUsage()
    {
      Console.WriteLine(
        "Usage: CfxMaestroSidecar.exe [--service-url URL] [--listen PREFIX] [--callback-base URL]\n" +
        "  --service-url    CFX Maestro WCF endpoint (default localhost:8003 ...)\n" +
        "  --listen         HttpListener prefix PyLabRobot connects to (default http://+:8080/)\n" +
        "  --callback-base  Local base URI for the WSDualHttpBinding duplex callback\n" +
        "                   (default http://localhost:8081/cfx-callback)");
    }
  }
}

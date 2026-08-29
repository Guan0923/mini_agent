using System;
using System.IO;
using System.Net.Sockets;
using System.Text;

internal static class SandboxNetworkProbe
{
    private static int Main(string[] args)
    {
        if (args.Length != 3 || (args[2] != "proxy" && args[2] != "direct"))
        {
            return 2;
        }

        string targetHost = args[0];
        int targetPort;
        if (!Int32.TryParse(args[1], out targetPort))
        {
            return 2;
        }

        try
        {
            string connectHost = targetHost;
            int connectPort = targetPort;
            string requestTarget = "/";
            string proxyHeader = "";
            if (args[2] == "proxy")
            {
                Uri proxy = new Uri(Environment.GetEnvironmentVariable("HTTP_PROXY"));
                connectHost = proxy.Host;
                connectPort = proxy.Port;
                requestTarget = "http://" + targetHost + ":" + targetPort + "/";
                string userInfo = Uri.UnescapeDataString(proxy.UserInfo);
                string encoded = Convert.ToBase64String(Encoding.UTF8.GetBytes(userInfo));
                proxyHeader = "Proxy-Authorization: Basic " + encoded + "\r\n";
            }

            using (TcpClient client = new TcpClient())
            {
                client.Connect(connectHost, connectPort);
                using (NetworkStream stream = client.GetStream())
                {
                    string request =
                        "GET " + requestTarget + " HTTP/1.1\r\n" +
                        "Host: " + targetHost + ":" + targetPort + "\r\n" +
                        proxyHeader +
                        "Connection: close\r\n\r\n";
                    byte[] requestBytes = Encoding.ASCII.GetBytes(request);
                    stream.Write(requestBytes, 0, requestBytes.Length);
                    using (StreamReader reader = new StreamReader(stream, Encoding.ASCII))
                    {
                        string response = reader.ReadToEnd();
                        if (response.IndexOf("sandbox-ok", StringComparison.Ordinal) < 0)
                        {
                            return 22;
                        }
                    }
                }
            }
            Console.WriteLine("sandbox-ok");
            return 0;
        }
        catch
        {
            return 23;
        }
    }
}

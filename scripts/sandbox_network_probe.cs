using System;
using System.IO;
using System.Net.Sockets;
using System.Text;

internal static class SandboxNetworkProbe
{
    private static int Main(string[] args)
    {
        if (args.Length != 2 || (args[1] != "proxy" && args[1] != "direct"))
        {
            return 2;
        }

        int targetPort;
        if (!Int32.TryParse(args[0], out targetPort))
        {
            return 2;
        }

        try
        {
            string connectHost = "127.0.0.1";
            int connectPort = targetPort;
            string requestTarget = "/";
            string proxyHeader = "";
            if (args[1] == "proxy")
            {
                Uri proxy = new Uri(Environment.GetEnvironmentVariable("HTTP_PROXY"));
                connectHost = proxy.Host;
                connectPort = proxy.Port;
                requestTarget = "http://127.0.0.1:" + targetPort + "/";
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
                        "Host: 127.0.0.1:" + targetPort + "\r\n" +
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

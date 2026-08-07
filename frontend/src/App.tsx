import { App as AntApp, ConfigProvider } from "antd";
import zhCN from "antd/locale/zh_CN";
import { BrowserRouter } from "react-router-dom";
import { AuthProvider } from "./auth/AuthProvider";
import AppRoutes from "./app/routes";
import { oceanTheme } from "./app/theme";

export { countUnreadArchived, loadArchiveReadState, loadConversations, markArchivedAsRead } from "./app/storage";

export default function App() {
  return <ConfigProvider locale={zhCN} theme={oceanTheme}><AntApp><BrowserRouter><AuthProvider><AppRoutes /></AuthProvider></BrowserRouter></AntApp></ConfigProvider>;
}

import { App as AntApp, ConfigProvider } from "antd";
import zhCN from "antd/locale/zh_CN";
import { BrowserRouter } from "react-router-dom";
import AppRoutes from "./app/routes";
import { oceanTheme } from "./app/theme";

export { countUnreadArchived, loadArchiveReadState, markArchivedAsRead } from "./app/storage";

export default function App() {
  return <ConfigProvider locale={zhCN} theme={oceanTheme}><AntApp><BrowserRouter><AppRoutes /></BrowserRouter></AntApp></ConfigProvider>;
}

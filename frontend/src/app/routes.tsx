import { Navigate, Route, Routes } from "react-router-dom";
import AgentApp from "./AgentApp";

export default function AppRoutes() {
  return (
    <Routes>
      <Route path="/" element={<AgentApp />} />
      <Route path="*" element={<Navigate to="/" replace />} />
    </Routes>
  );
}

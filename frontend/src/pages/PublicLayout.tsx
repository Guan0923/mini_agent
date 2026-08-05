import { Outlet } from "react-router-dom";
import OceanScene from "../components/OceanScene";

/** Keeps the public ocean scene mounted while foreground routes change. */
export default function PublicLayout() {
  return (
    <div className="public-shell">
      <OceanScene />
      <Outlet />
    </div>
  );
}

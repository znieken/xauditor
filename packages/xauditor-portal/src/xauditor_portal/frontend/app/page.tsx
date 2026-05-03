import { redirect } from "next/navigation";

export default function LandingPage() {
  // The landing route forwards to the authenticated shell; if the session
  // cookie is missing, the authenticated layout redirects to /login.
  redirect("/reports");
}

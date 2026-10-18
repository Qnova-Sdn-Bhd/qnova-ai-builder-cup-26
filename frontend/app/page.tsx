import { Guardian } from "../components/guardian";
export const dynamic = "force-dynamic";
export default function Home() {
  return <Guardian config={{
    apiBaseUrl: process.env.NEXT_PUBLIC_API_BASE_URL || "",
    apiKey: process.env.NEXT_PUBLIC_FIREBASE_API_KEY || "",
    authDomain: process.env.NEXT_PUBLIC_FIREBASE_AUTH_DOMAIN || "",
    projectId: process.env.NEXT_PUBLIC_FIREBASE_PROJECT_ID || "",
    appId: process.env.NEXT_PUBLIC_FIREBASE_APP_ID || "",
  }} />;
}

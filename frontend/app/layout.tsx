import type { Metadata } from "next";
import "./globals.css";
export const metadata: Metadata = { title: "Handoff Guardian · Delivery confidence, with evidence", description: "Reconcile customer promises, accepted scope, and delivery evidence before handoff." };
export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return <html lang="en"><body>{children}</body></html>;
}

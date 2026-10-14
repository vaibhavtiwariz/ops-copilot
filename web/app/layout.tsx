import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "ops·copilot",
  description: "Supervised multi-agent operations console",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en" className="dark">
      <body className="bg-ink-950 antialiased">{children}</body>
    </html>
  );
}

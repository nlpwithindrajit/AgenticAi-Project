import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "AI Travel Planner",
  description:
    "Multi-agent trip planning: flights, hotels, activities and a costed itinerary.",
};

export default function RootLayout({
  children,
}: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="en">
      <body>
        <header className="site-header">
          <div className="wrap">
            <span className="mark">✈</span>
            <h1>AI Travel Planner</h1>
            <span className="tagline">
              search &rarr; filter &rarr; rank &rarr; recommend
            </span>
          </div>
        </header>
        <main className="wrap">{children}</main>
        <footer className="wrap site-footer">
          <p>
            Search only — no booking. Prices marked <em>estimated</em> are not
            quotes.
          </p>
        </footer>
      </body>
    </html>
  );
}

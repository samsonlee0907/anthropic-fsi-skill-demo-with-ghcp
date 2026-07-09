import type { Metadata, Viewport } from 'next';
import './globals.css';

export const metadata: Metadata = {
  title: 'FSI Multi-Agent Studio',
  description: 'A financial-services multi-agent demo powered by Microsoft Foundry Agent Service.'
};

export const viewport: Viewport = {
  themeColor: '#0B2545'
};

export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}

import type { Metadata } from 'next';
import { Inter } from 'next/font/google';
import './globals.css';

const inter = Inter({ subsets: ['latin'] });

export const metadata: Metadata = {
  title: 'Research Paper Hub',
  description: '멀티사이트 논문/발간물 큐레이션',
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="ko">
      <body className={`${inter.className} bg-gray-50 min-h-screen`}>
        <header className="bg-white border-b border-gray-200">
          <div className="max-w-6xl mx-auto px-4 py-4 flex items-center justify-between">
            <div>
              <a href="/" className="text-2xl font-bold text-gray-900 hover:text-blue-600 transition-colors">
                Research Paper Hub
              </a>
              <p className="text-sm text-gray-500 mt-1">논문 및 발간물 큐레이션</p>
            </div>
            <nav className="flex gap-4">
              <a href="/" className="text-sm font-medium text-gray-600 hover:text-blue-600 transition-colors">
                사이트 목록
              </a>
              <a href="/papers" className="text-sm font-medium text-gray-600 hover:text-blue-600 transition-colors">
                전체 문서
              </a>
            </nav>
          </div>
        </header>
        <main className="max-w-6xl mx-auto px-4 py-6">{children}</main>
      </body>
    </html>
  );
}

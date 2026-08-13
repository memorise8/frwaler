import { Noto_Sans_KR, Noto_Serif_KR } from "next/font/google";

export const sansKR = Noto_Sans_KR({
  subsets: ["latin"],
  display: "swap",
  variable: "--font-sans",
  fallback: ["Malgun Gothic", "Apple SD Gothic Neo", "sans-serif"],
});

export const serifKR = Noto_Serif_KR({
  subsets: ["latin"],
  display: "swap",
  variable: "--font-serif",
  fallback: ["Iowan Old Style", "AppleMyungjo", "serif"],
});

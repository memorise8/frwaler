/** @type {import('next').NextConfig} */
const API_URL = process.env.NEXT_PUBLIC_API_URL || "http://localhost:30004";
const PRO_API_URL = process.env.NEXT_PUBLIC_PRO_API_URL || "http://localhost:30005";

const nextConfig = {
  async rewrites() {
    return [
      {
        source: "/api/:path*",
        destination: `${API_URL}/api/:path*`,
      },
      {
        source: "/pro/api/:path*",
        destination: `${PRO_API_URL}/pro/api/:path*`,
      },
    ];
  },
};

module.exports = nextConfig;

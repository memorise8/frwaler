/** @type {import('next').NextConfig} */
const nextConfig = {
  async rewrites() {
    return [
      {
        source: "/api/:path*",
        destination: "http://localhost:30004/api/:path*",
      },
    ];
  },
};

module.exports = nextConfig;

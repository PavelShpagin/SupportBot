/** @type {import('next').NextConfig} */
const nextConfig = {
  output: 'standalone',
  reactStrictMode: true,
  images: {
    remotePatterns: [
      {
        protocol: 'http',
        hostname: 'localhost',
        port: '8000',
        pathname: '/static/**',
      },
      {
        protocol: 'http',
        hostname: '161.33.64.115',
        port: '8000',
        pathname: '/static/**',
      },
    ],
  },
};

module.exports = nextConfig;

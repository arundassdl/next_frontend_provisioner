/** @type {import('next').NextConfig} */
const nextConfig = {
  output: 'standalone',
  images: {
    remotePatterns: [{
      protocol: 'https',
      hostname: process.env.FRAPPE_HOSTNAME || 'localhost',
    }],
  },
  env: {
    NEXT_PUBLIC_FRAPPE_URL: process.env.NEXT_PUBLIC_FRAPPE_URL,
  },
}
module.exports = nextConfig

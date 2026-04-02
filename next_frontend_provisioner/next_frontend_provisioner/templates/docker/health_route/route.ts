import { NextResponse } from 'next/server'

// Injected by next_frontend_provisioner — do not remove.
// Required for container health checks.
export async function GET() {
  return NextResponse.json({
    status: 'ok',
    ts: Date.now(),
  })
}
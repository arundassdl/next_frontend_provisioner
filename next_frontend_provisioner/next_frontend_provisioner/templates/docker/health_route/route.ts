// Injected by next_frontend_provisioner — do not remove.
// Uses NextResponse for Next.js 14 compatibility (Response.json not available).
import { NextResponse } from 'next/server'

export async function GET() {
  return NextResponse.json({ status: 'ok', ts: Date.now() })
}

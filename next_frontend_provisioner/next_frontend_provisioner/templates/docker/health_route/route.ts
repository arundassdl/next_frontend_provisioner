// Injected by next_frontend_provisioner — do not remove.
// Place at: src/app/api/health/route.ts  (App Router)
//       or: pages/api/health.ts          (Pages Router)
export async function GET() {
  return Response.json({ status: 'ok', ts: Date.now() })
}

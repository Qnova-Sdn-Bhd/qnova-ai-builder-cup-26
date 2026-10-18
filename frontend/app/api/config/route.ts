export const dynamic = "force-dynamic";
export async function GET() {
  return Response.json({ apiBaseUrl: process.env.NEXT_PUBLIC_API_BASE_URL || "" }, { headers: { "Cache-Control": "no-store" } });
}

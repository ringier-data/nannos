import { useEffect, useState } from "react"
import { Link, Outlet } from "react-router"
import { useTheme } from "@/components/theme-provider"
import { Button } from "@/components/ui/button"
import { LogOut, Moon, Sun } from "lucide-react"
import { client } from "@/api/generated/client.gen"

export function App() {
  const { theme, setTheme } = useTheme()
  const [ready, setReady] = useState(false)
  const [email, setEmail] = useState<string | null>(null)

  useEffect(() => {
    client
      .get({ url: "/api/v2/auth/me" })
      .then((res) => {
        if (res.response?.ok && res.data) {
          const data = res.data as { email?: string }
          setEmail(data.email ?? null)
          setReady(true)
        }
        // 401 is handled by the interceptor (redirect to login)
      })
      .catch(() => {
        // network error — interceptor may have already redirected for 401
      })
  }, [])

  if (!ready) {
    return (
      <div className="flex min-h-svh items-center justify-center">
        <p className="text-muted-foreground">Loading…</p>
      </div>
    )
  }

  return (
    <div className="min-h-svh">
      <header className="border-b px-6 py-3">
        <nav className="flex items-center gap-6">
          <Link to="/" className="text-lg font-semibold">
            Nannos Admin
          </Link>
          <Link
            to="/"
            className="text-sm text-muted-foreground hover:text-foreground"
          >
            Installations
          </Link>
          <div className="ml-auto flex items-center gap-2">
            {email && (
              <span className="text-sm text-muted-foreground">{email}</span>
            )}
            <Button
              variant="ghost"
              size="icon"
              onClick={() => setTheme(theme === "dark" ? "light" : "dark")}
            >
              {theme === "dark" ? (
                <Sun className="size-4" />
              ) : (
                <Moon className="size-4" />
              )}
              <span className="sr-only">Toggle theme</span>
            </Button>
            <Button
              variant="ghost"
              size="icon"
              onClick={() => {
                window.location.href = `/api/v2/auth/logout?redirectTo=${encodeURIComponent(window.location.href)}`
              }}
            >
              <LogOut className="size-4" />
              <span className="sr-only">Logout</span>
            </Button>
          </div>
        </nav>
      </header>
      <main className="mx-auto max-w-5xl p-6">
        <Outlet />
      </main>
    </div>
  )
}

export default App

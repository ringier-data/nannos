import { useSearchParams, Link } from "react-router"
import { useTheme } from "@/components/theme-provider"
import { Button } from "@/components/ui/button"
import { AlertTriangle, ShieldX, LogIn, Moon, Sun } from "lucide-react"

const ERROR_CONFIG: Record<string, { icon: typeof AlertTriangle; title: string }> = {
  access_denied: { icon: ShieldX, title: "No permissions" },
}

const DEFAULT_ERROR = { icon: AlertTriangle, title: "Authentication error" }

export function AuthErrorPage() {
  const [searchParams] = useSearchParams()
  const { theme, setTheme } = useTheme()

  const errorCode = searchParams.get("error") ?? "unknown"
  const message =
    searchParams.get("message") ?? "An unexpected error occurred during login."

  const { icon: Icon, title } = ERROR_CONFIG[errorCode] ?? DEFAULT_ERROR

  return (
    <div className="min-h-svh">
      <header className="border-b px-6 py-3">
        <nav className="flex items-center gap-6">
          <span className="text-lg font-semibold">Nannos Admin</span>
          <div className="ml-auto">
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
          </div>
        </nav>
      </header>
      <main className="flex flex-col items-center justify-center gap-6 px-6 py-24">
        <Icon className="size-16 text-destructive" />
        <h1 className="text-3xl font-semibold">{title}</h1>
        <p className="max-w-md text-center text-muted-foreground">{message}</p>
        <Link to="/">
          <Button>
            <LogIn className="mr-2 size-4" />
            Try again
          </Button>
        </Link>
      </main>
    </div>
  )
}

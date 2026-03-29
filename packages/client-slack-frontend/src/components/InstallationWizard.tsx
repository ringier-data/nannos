import { useState } from "react"
import { client } from "@/api/generated/client.gen"
import type { InstallationFormData } from "@/types/installation"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { Textarea } from "@/components/ui/textarea"
import { AvatarUpload } from "@/components/AvatarUpload"
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card"
import { Download, ExternalLink, ChevronRight, ChevronLeft } from "lucide-react"

interface Props {
  onComplete: (data: InstallationFormData) => Promise<void>
  onCancel: () => void
}

type Step = "configure" | "create-app" | "credentials"

const STEPS: { key: Step; label: string }[] = [
  { key: "configure", label: "Configure Bot" },
  { key: "create-app", label: "Create Slack App" },
  { key: "credentials", label: "Enter Credentials" },
]

export function InstallationWizard({ onComplete, onCancel }: Props) {
  const [step, setStep] = useState<Step>("configure")
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState<string | null>(null)

  // Step 1: bot configuration
  const [botName, setBotName] = useState("")
  const [slashCommand, setSlashCommand] = useState("/")
  const [slashCommandManual, setSlashCommandManual] = useState(false)
  const [botDescription, setBotDescription] = useState("")

  const deriveSlashCommand = (name: string) =>
    "/" +
    name
      .toLowerCase()
      .replace(/\s+/g, "-")
      .replace(/[^a-z0-9-]/g, "")

  const handleBotNameChange = (name: string) => {
    setBotName(name)
    if (!slashCommandManual) {
      setSlashCommand(deriveSlashCommand(name))
    }
  }

  const handleSlashCommandChange = (value: string) => {
    setSlashCommandManual(true)
    setSlashCommand(value)
  }
  const [socketMode, setSocketMode] = useState(false)

  // Step 2: generated manifest
  const [manifest, setManifest] = useState<object | null>(null)
  const [manifestLoading, setManifestLoading] = useState(false)

  // Step 3: credentials from created Slack App
  const [appId, setAppId] = useState("")
  const [teamId, setTeamId] = useState("")
  const [botToken, setBotToken] = useState("")
  const [signingSecret, setSigningSecret] = useState("")
  const [avatarFile, setAvatarFile] = useState<File | null>(null)

  const currentIndex = STEPS.findIndex((s) => s.key === step)

  const generateManifest = async () => {
    setManifestLoading(true)
    setError(null)
    try {
      const res = await client.post({
        url: "/api/v2/installations/manifest",
        body: {
          botName,
          slashCommand,
          ...(botDescription ? { botDescription } : {}),
          ...(socketMode ? { socketMode: true } : {}),
        },
      })
      if (res.error) throw new Error("Failed to generate manifest")
      setManifest((res.data as { manifest: object }).manifest)
      setStep("create-app")
    } catch (err) {
      setError(
        err instanceof Error ? err.message : "Failed to generate manifest"
      )
    } finally {
      setManifestLoading(false)
    }
  }

  const downloadManifest = () => {
    if (!manifest) return
    const blob = new Blob([JSON.stringify(manifest, null, 2)], {
      type: "application/json",
    })
    const url = URL.createObjectURL(blob)
    const a = document.createElement("a")
    a.href = url
    a.download = `slack-app-manifest-${botName.toLowerCase().replace(/\s+/g, "-")}.json`
    a.click()
    URL.revokeObjectURL(url)
  }

  const handleFinish = async () => {
    setSubmitting(true)
    setError(null)
    try {
      await onComplete({
        appId,
        teamId,
        botToken,
        signingSecret,
        botName,
        avatarUrl: "",
        slashCommand,
      })
      // Upload avatar after installation is created
      if (avatarFile) {
        const formData = new FormData()
        formData.append("avatar", avatarFile)
        await fetch(
          `/api/v2/installations/${encodeURIComponent(appId)}/avatar`,
          {
            method: "POST",
            body: formData,
          }
        )
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : "An error occurred")
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <div className="space-y-6">
      {/* Step indicator */}
      <nav className="flex items-center gap-2 text-sm">
        {STEPS.map((s, i) => (
          <div key={s.key} className="flex items-center gap-2">
            {i > 0 && (
              <ChevronRight className="size-3.5 text-muted-foreground" />
            )}
            <span
              className={
                s.key === step
                  ? "font-medium text-foreground"
                  : i < currentIndex
                    ? "text-muted-foreground"
                    : "text-muted-foreground/60"
              }
            >
              {i + 1}. {s.label}
            </span>
          </div>
        ))}
      </nav>

      {error && <p className="text-sm text-red-500">{error}</p>}

      {/* Step 1: Configure */}
      {step === "configure" && (
        <Card>
          <CardHeader>
            <CardTitle>Configure Your Bot</CardTitle>
            <CardDescription>
              Choose a name and slash command for your new Slack bot. A manifest
              will be generated so you can create the app in Slack.
            </CardDescription>
          </CardHeader>
          <CardContent>
            <div className="space-y-4">
              <div className="grid gap-4 sm:grid-cols-2">
                <div className="space-y-2">
                  <Label htmlFor="wiz-botName">Bot Name</Label>
                  <Input
                    id="wiz-botName"
                    value={botName}
                    onChange={(e) => handleBotNameChange(e.target.value)}
                    placeholder="My Team Bot"
                    required
                  />
                  <p className="text-xs text-muted-foreground">
                    Display name shown in Slack messages
                  </p>
                </div>
                <div className="space-y-2">
                  <Label htmlFor="wiz-slashCommand">Slash Command</Label>
                  <Input
                    id="wiz-slashCommand"
                    value={slashCommand}
                    onChange={(e) => handleSlashCommandChange(e.target.value)}
                    placeholder="/my-bot"
                    required
                  />
                  <p className="text-xs text-muted-foreground">
                    Auto-derived from bot name — edit to override
                  </p>
                </div>
                <div className="space-y-2 sm:col-span-2">
                  <Label htmlFor="wiz-botDescription">
                    Description (optional)
                  </Label>
                  <Input
                    id="wiz-botDescription"
                    value={botDescription}
                    onChange={(e) => setBotDescription(e.target.value)}
                    placeholder="A brief description of what this bot does"
                  />
                </div>
              </div>

              <label className="flex cursor-pointer items-center gap-2">
                <input
                  type="checkbox"
                  checked={socketMode}
                  onChange={(e) => setSocketMode(e.target.checked)}
                  className="size-4 rounded border-input accent-primary"
                />
                <span className="text-sm font-medium">Socket Mode</span>
                <span className="text-xs text-muted-foreground">
                  (local development — no public URL required)
                </span>
              </label>

              <div className="flex gap-2 pt-2">
                <Button
                  onClick={generateManifest}
                  disabled={!botName || !slashCommand || manifestLoading}
                >
                  {manifestLoading
                    ? "Generating..."
                    : "Next: Generate Manifest"}
                  {!manifestLoading && <ChevronRight className="size-4" />}
                </Button>
                <Button variant="outline" onClick={onCancel}>
                  Cancel
                </Button>
              </div>
            </div>
          </CardContent>
        </Card>
      )}

      {/* Step 2: Create Slack App */}
      {step === "create-app" && manifest && (
        <Card>
          <CardHeader>
            <CardTitle>Create Your Slack App</CardTitle>
            <CardDescription>
              Use the generated manifest below to create a new Slack App. Follow
              the steps, then copy the credentials into the next screen.
            </CardDescription>
          </CardHeader>
          <CardContent>
            <div className="space-y-5">
              {/* Instructions */}
              <ol className="list-inside list-decimal space-y-3 text-sm">
                <li>Download the manifest file or copy the JSON below.</li>
                <li>
                  Go to{" "}
                  <a
                    href="https://api.slack.com/apps?new_app=1"
                    target="_blank"
                    rel="noopener noreferrer"
                    className="inline-flex items-center gap-1 font-medium text-primary underline underline-offset-3 hover:text-primary/80"
                  >
                    api.slack.com/apps
                    <ExternalLink className="size-3" />
                  </a>{" "}
                  and click <strong>Create New App</strong> →{" "}
                  <strong>From a manifest</strong>.
                </li>
                <li>Select your workspace and paste or upload the manifest.</li>
                <li>
                  Review the configuration and click <strong>Create</strong>.
                </li>
                <li>
                  Under <strong>Basic Information → App Credentials</strong>,
                  copy the <strong>Signing Secret</strong>.
                </li>
                <li>
                  Under <strong>OAuth & Permissions</strong>, click{" "}
                  <strong>Install to Workspace</strong> and copy the{" "}
                  <strong>Bot User OAuth Token</strong> (starts with{" "}
                  <code className="rounded bg-muted px-1 py-0.5 text-xs">
                    xoxb-
                  </code>
                  ).
                </li>
                <li>
                  Note the <strong>App ID</strong> (from Basic Information) and
                  your <strong>Team ID</strong> (workspace ID).
                </li>
                {socketMode && (
                  <li>
                    Under <strong>Basic Information → App-Level Tokens</strong>,
                    generate a token with the{" "}
                    <code className="rounded bg-muted px-1 py-0.5 text-xs">
                      connections:write
                    </code>{" "}
                    scope. This is the <strong>App Token</strong> (starts with{" "}
                    <code className="rounded bg-muted px-1 py-0.5 text-xs">
                      xapp-
                    </code>
                    ).
                  </li>
                )}
              </ol>

              {socketMode && (
                <div className="rounded-lg border border-amber-200 bg-amber-50 px-3 py-2 text-xs text-amber-800 dark:border-amber-800 dark:bg-amber-950 dark:text-amber-200">
                  <strong>Socket Mode</strong> — This manifest has no webhook
                  URLs. The app will connect via WebSocket and requires an App
                  Token to run.
                </div>
              )}

              {/* Manifest preview */}
              <div className="space-y-2">
                <div className="flex items-center justify-between">
                  <Label>Generated Manifest</Label>
                  <Button
                    variant="outline"
                    size="sm"
                    onClick={downloadManifest}
                  >
                    <Download className="size-3.5" />
                    Download JSON
                  </Button>
                </div>
                <Textarea
                  readOnly
                  rows={12}
                  className="font-mono text-xs"
                  value={JSON.stringify(manifest, null, 2)}
                />
              </div>

              <div className="flex gap-2 pt-2">
                <Button variant="outline" onClick={() => setStep("configure")}>
                  <ChevronLeft className="size-4" />
                  Back
                </Button>
                <Button onClick={() => setStep("credentials")}>
                  Next: Enter Credentials
                  <ChevronRight className="size-4" />
                </Button>
              </div>
            </div>
          </CardContent>
        </Card>
      )}

      {/* Step 3: Enter Credentials */}
      {step === "credentials" && (
        <Card>
          <CardHeader>
            <CardTitle>Enter Slack App Credentials</CardTitle>
            <CardDescription>
              Paste the credentials from the Slack App you just created.
            </CardDescription>
          </CardHeader>
          <CardContent>
            <div className="space-y-4">
              <div className="grid gap-4 sm:grid-cols-2">
                <div className="space-y-2">
                  <Label htmlFor="wiz-appId">App ID</Label>
                  <Input
                    id="wiz-appId"
                    value={appId}
                    onChange={(e) => setAppId(e.target.value)}
                    placeholder="A0123456789"
                    required
                  />
                  <p className="text-xs text-muted-foreground">
                    Found in Basic Information
                  </p>
                </div>
                <div className="space-y-2">
                  <Label htmlFor="wiz-teamId">Team ID (Workspace)</Label>
                  <Input
                    id="wiz-teamId"
                    value={teamId}
                    onChange={(e) => setTeamId(e.target.value)}
                    placeholder="T0123456789"
                    required
                  />
                </div>
                <div className="space-y-2 sm:col-span-2">
                  <Label htmlFor="wiz-botToken">Bot User OAuth Token</Label>
                  <Input
                    id="wiz-botToken"
                    value={botToken}
                    onChange={(e) => setBotToken(e.target.value)}
                    placeholder="xoxb-..."
                    required
                  />
                </div>
                <div className="space-y-2 sm:col-span-2">
                  <Label htmlFor="wiz-signingSecret">Signing Secret</Label>
                  <Input
                    id="wiz-signingSecret"
                    value={signingSecret}
                    onChange={(e) => setSigningSecret(e.target.value)}
                    required
                  />
                </div>
                <div className="space-y-2 sm:col-span-2">
                  <AvatarUpload file={avatarFile} onChange={setAvatarFile} />
                </div>
              </div>

              <div className="flex gap-2 pt-2">
                <Button variant="outline" onClick={() => setStep("create-app")}>
                  <ChevronLeft className="size-4" />
                  Back
                </Button>
                <Button
                  onClick={handleFinish}
                  disabled={
                    !appId ||
                    !teamId ||
                    !botToken ||
                    !signingSecret ||
                    submitting
                  }
                >
                  {submitting ? "Creating..." : "Create Installation"}
                </Button>
              </div>
            </div>
          </CardContent>
        </Card>
      )}
    </div>
  )
}

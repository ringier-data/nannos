import { useState } from "react"
import type { InstallationFormData } from "@/types/installation"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { AvatarUpload } from "@/components/AvatarUpload"
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card"

interface Props {
  mode: "create" | "edit"
  initialData?: Partial<InstallationFormData>
  onSubmit: (data: InstallationFormData) => Promise<void>
  onCancel: () => void
}

export function InstallationForm({
  mode,
  initialData,
  onSubmit,
  onCancel,
}: Props) {
  const [formData, setFormData] = useState<InstallationFormData>({
    appId: initialData?.appId ?? "",
    teamId: initialData?.teamId ?? "",
    botToken: initialData?.botToken ?? "",
    signingSecret: initialData?.signingSecret ?? "",
    botName: initialData?.botName ?? "",
    avatarUrl: initialData?.avatarUrl ?? "",
    slashCommand: initialData?.slashCommand ?? "/nannos",
  })
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [avatarFile, setAvatarFile] = useState<File | null>(null)

  const handleChange = (field: keyof InstallationFormData, value: string) => {
    setFormData((prev) => ({ ...prev, [field]: value }))
  }

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault()
    setSubmitting(true)
    setError(null)
    try {
      await onSubmit(formData)
      // Upload avatar if a new file was selected
      if (avatarFile && formData.appId) {
        const fd = new FormData()
        fd.append("avatar", avatarFile)
        await fetch(
          `/api/v2/installations/${encodeURIComponent(formData.appId)}/avatar`,
          {
            method: "POST",
            body: fd,
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
    <Card>
      <CardHeader>
        <CardTitle>
          {mode === "create" ? "New Installation" : "Edit Installation"}
        </CardTitle>
        <CardDescription>
          {mode === "create"
            ? "Enter the credentials from your Slack App configuration. You can find these in the Slack API dashboard under your app's settings."
            : "Update the installation configuration."}
        </CardDescription>
      </CardHeader>
      <CardContent>
        <form onSubmit={handleSubmit} className="space-y-4">
          {error && <p className="text-sm text-red-500">{error}</p>}

          <div className="grid gap-4 sm:grid-cols-2">
            <div className="space-y-2">
              <Label htmlFor="appId">App ID</Label>
              <Input
                id="appId"
                value={formData.appId}
                onChange={(e) => handleChange("appId", e.target.value)}
                placeholder="A0123456789"
                required
                disabled={mode === "edit"}
              />
            </div>

            <div className="space-y-2">
              <Label htmlFor="teamId">Team ID (Workspace)</Label>
              <Input
                id="teamId"
                value={formData.teamId}
                onChange={(e) => handleChange("teamId", e.target.value)}
                placeholder="T0123456789"
                required
              />
            </div>

            <div className="space-y-2 sm:col-span-2">
              <Label htmlFor="botToken">Bot Token</Label>
              <Input
                id="botToken"
                value={formData.botToken}
                onChange={(e) => handleChange("botToken", e.target.value)}
                placeholder="xoxb-..."
                required
              />
            </div>

            <div className="space-y-2 sm:col-span-2">
              <Label htmlFor="signingSecret">Signing Secret</Label>
              <Input
                id="signingSecret"
                value={formData.signingSecret}
                onChange={(e) => handleChange("signingSecret", e.target.value)}
                required
              />
            </div>

            <div className="space-y-2">
              <Label htmlFor="botName">Bot Name</Label>
              <Input
                id="botName"
                value={formData.botName}
                onChange={(e) => handleChange("botName", e.target.value)}
                placeholder="Nannos"
                required
              />
            </div>

            <div className="space-y-2">
              <Label htmlFor="slashCommand">Slash Command</Label>
              <Input
                id="slashCommand"
                value={formData.slashCommand}
                onChange={(e) => handleChange("slashCommand", e.target.value)}
                placeholder="/nannos"
                required
              />
            </div>

            <div className="space-y-2 sm:col-span-2">
              <AvatarUpload
                appId={mode === "edit" ? formData.appId : undefined}
                file={avatarFile}
                onChange={setAvatarFile}
              />
            </div>
          </div>

          <div className="flex gap-2 pt-4">
            <Button type="submit" disabled={submitting}>
              {submitting
                ? "Saving..."
                : mode === "create"
                  ? "Create Installation"
                  : "Save Changes"}
            </Button>
            <Button type="button" variant="outline" onClick={onCancel}>
              Cancel
            </Button>
          </div>
        </form>
      </CardContent>
    </Card>
  )
}

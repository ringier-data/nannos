import { useEffect, useState } from "react"
import { useNavigate, useParams } from "react-router"
import { client } from "@/api/generated/client.gen"
import type { Installation, InstallationFormData } from "@/types/installation"
import { InstallationForm } from "@/components/InstallationForm"

export function EditInstallationPage() {
  const { appId } = useParams<{ appId: string }>()
  const navigate = useNavigate()
  const [installation, setInstallation] = useState<Installation | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    if (!appId) return
    client
      .get({
        url: "/api/v2/installations/{appId}",
        path: { appId },
      })
      .then((res) => {
        const inst = (res.data as { installation: Installation })?.installation
        if (!inst) throw new Error("Installation not found")
        setInstallation(inst)
      })
      .catch((err) => {
        setError(
          err instanceof Error ? err.message : "Failed to load installation"
        )
      })
      .finally(() => setLoading(false))
  }, [appId])

  const handleSubmit = async (data: InstallationFormData) => {
    if (!appId) return
    // Spelled out rather than spread from `data`: the form is shared with the create flow, so
    // it carries `appId`, which here belongs in the path and is rejected as an unknown property
    // by the update schema. Listing the fields keeps that contract visible at the call site.
    const body = {
      teamId: data.teamId,
      botToken: data.botToken,
      signingSecret: data.signingSecret,
      botName: data.botName,
      slashCommand: data.slashCommand,
      avatarUrl: data.avatarUrl || undefined,
    }
    const res = await client.put({
      url: "/api/v2/installations/{appId}",
      path: { appId },
      body,
    })
    if (res.error) {
      throw new Error(
        (res.error as { error?: string })?.error ?? "Failed to update"
      )
    }
    navigate("/")
  }

  if (loading) {
    return <p className="text-sm text-muted-foreground">Loading...</p>
  }

  if (error || !installation) {
    return <p className="text-sm text-red-500">{error ?? "Not found"}</p>
  }

  return (
    <div className="space-y-6">
      <h1 className="text-2xl font-semibold">Edit Installation</h1>
      <InstallationForm
        mode="edit"
        initialData={{
          appId: installation.appId,
          teamId: installation.teamId,
          botToken: installation.botToken,
          signingSecret: installation.signingSecret,
          botName: installation.botName,
          avatarUrl: installation.avatarUrl ?? "",
          slashCommand: installation.slashCommand,
        }}
        onSubmit={handleSubmit}
        onCancel={() => navigate("/")}
      />
    </div>
  )
}

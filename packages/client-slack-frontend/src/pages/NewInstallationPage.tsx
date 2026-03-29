import { useNavigate } from "react-router"
import { client } from "@/api/generated/client.gen"
import type { InstallationFormData } from "@/types/installation"
import { InstallationWizard } from "@/components/InstallationWizard"

export function NewInstallationPage() {
  const navigate = useNavigate()

  const handleComplete = async (data: InstallationFormData) => {
    const body = {
      ...data,
      avatarUrl: data.avatarUrl || undefined,
    }
    const res = await client.post({
      url: "/api/v2/installations",
      body,
    })
    if (res.error) {
      throw new Error(
        (res.error as { error?: string })?.error ?? "Failed to create"
      )
    }
    navigate("/")
  }

  return (
    <div className="space-y-6">
      <h1 className="text-2xl font-semibold">Add Installation</h1>
      <InstallationWizard
        onComplete={handleComplete}
        onCancel={() => navigate("/")}
      />
    </div>
  )
}

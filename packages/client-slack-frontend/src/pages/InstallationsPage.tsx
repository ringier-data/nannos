import { useEffect, useState } from "react"
import { Link } from "react-router"
import { client } from "@/api/generated/client.gen"
import type { Installation } from "@/types/installation"
import { Button } from "@/components/ui/button"
import { Badge } from "@/components/ui/badge"
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table"
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"

export function InstallationsPage() {
  const [installations, setInstallations] = useState<Installation[]>([])
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)
  const [deleteTarget, setDeleteTarget] = useState<Installation | null>(null)

  const fetchInstallations = async () => {
    setLoading(true)
    try {
      const res = await client.get({ url: "/api/v2/installations" })
      setInstallations(
        (res.data as { installations: Installation[] })?.installations ?? []
      )
      setError(null)
    } catch (err) {
      setError(
        err instanceof Error ? err.message : "Failed to fetch installations"
      )
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    fetchInstallations()
  }, [])

  const handleDelete = async () => {
    if (!deleteTarget) return
    try {
      await client.delete({
        url: "/api/v2/installations/{appId}",
        path: { appId: deleteTarget.appId },
      })
      setDeleteTarget(null)
      fetchInstallations()
    } catch (err) {
      setError(
        err instanceof Error ? err.message : "Failed to delete installation"
      )
      setDeleteTarget(null)
    }
  }

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <h1 className="text-2xl font-semibold">Installations</h1>
        <Button asChild>
          <Link to="/installations/new">Add Installation</Link>
        </Button>
      </div>
      {error && <p className="text-sm text-red-500">{error}</p>}
      {loading ? (
        <p className="text-sm text-muted-foreground">Loading...</p>
      ) : installations.length === 0 ? (
        <div className="py-12 text-center text-sm text-muted-foreground">
          No installations found. Add one to get started.
        </div>
      ) : (
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead className="w-12"></TableHead>
              <TableHead>Slack Bot Name</TableHead>
              <TableHead>Slack App ID</TableHead>
              <TableHead>Slack Workspace ID</TableHead>
              <TableHead>Slash Command</TableHead>
              <TableHead>Status</TableHead>
              <TableHead className="text-right">Actions</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {installations.map((inst) => (
              <TableRow key={inst.appId}>
                <TableCell>
                  <img
                    src={`/api/v2/installations/${inst.appId}/avatar`}
                    alt=""
                    className="size-8 rounded-full border object-cover"
                    onError={(e) => {
                      ;(e.target as HTMLImageElement).style.display = "none"
                    }}
                  />
                </TableCell>
                <TableCell className="font-medium">{inst.botName}</TableCell>
                <TableCell className="font-mono text-xs">
                  {inst.appId}
                </TableCell>
                <TableCell className="font-mono text-xs">
                  {inst.teamId}
                </TableCell>
                <TableCell>{inst.slashCommand}</TableCell>
                <TableCell>
                  <Badge variant={inst.isActive ? "default" : "secondary"}>
                    {inst.isActive ? "Active" : "Inactive"}
                  </Badge>
                </TableCell>
                <TableCell className="text-right">
                  <div className="flex justify-end gap-2">
                    <Button variant="outline" size="sm" asChild>
                      <Link to={`/installations/${inst.appId}`}>Edit</Link>
                    </Button>
                    <Button
                      variant="destructive"
                      size="sm"
                      onClick={() => setDeleteTarget(inst)}
                    >
                      Deactivate
                    </Button>
                  </div>
                </TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      )}
      <Dialog
        open={!!deleteTarget}
        onOpenChange={(open) => !open && setDeleteTarget(null)}
      >
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Deactivate Installation</DialogTitle>
            <DialogDescription>
              This will deactivate the installation for{" "}
              <strong>{deleteTarget?.botName}</strong> ({deleteTarget?.appId}).
              The bot will stop responding in this workspace. This can be
              reversed by re-creating the installation.
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button variant="outline" onClick={() => setDeleteTarget(null)}>
              Cancel
            </Button>
            <Button variant="destructive" onClick={handleDelete}>
              Deactivate
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  )
}

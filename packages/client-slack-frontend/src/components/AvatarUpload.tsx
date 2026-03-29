import { useState, useRef } from "react"
import { Label } from "@/components/ui/label"
import { Upload, X } from "lucide-react"

interface Props {
  /** If set, shows existing avatar from server */
  appId?: string
  /** Called with the selected File, or null to remove */
  onChange: (file: File | null) => void
  /** Currently selected local file (for preview) */
  file: File | null
}

const ACCEPT = "image/png,image/jpeg,image/gif,image/webp,image/svg+xml"
const MAX_SIZE = 2 * 1024 * 1024

export function AvatarUpload({ appId, onChange, file }: Props) {
  const inputRef = useRef<HTMLInputElement>(null)
  const [dragOver, setDragOver] = useState(false)
  const [error, setError] = useState<string | null>(null)

  // Preview URL: local file takes precedence, then existing server avatar
  const previewUrl = file
    ? URL.createObjectURL(file)
    : appId
      ? `/api/v2/installations/${appId}/avatar`
      : null

  const handleFile = (f: File) => {
    setError(null)
    if (!ACCEPT.split(",").includes(f.type)) {
      setError("Unsupported file type. Use PNG, JPEG, GIF, WebP, or SVG.")
      return
    }
    if (f.size > MAX_SIZE) {
      setError("File too large. Maximum 2 MB.")
      return
    }
    onChange(f)
  }

  const handleInputChange = (e: React.ChangeEvent<HTMLInputElement>) => {
    const f = e.target.files?.[0]
    if (f) handleFile(f)
    // Reset so the same file can be re-selected
    e.target.value = ""
  }

  const handleDrop = (e: React.DragEvent) => {
    e.preventDefault()
    setDragOver(false)
    const f = e.dataTransfer.files[0]
    if (f) handleFile(f)
  }

  const handleRemove = () => {
    setError(null)
    onChange(null)
  }

  return (
    <div className="space-y-2">
      <Label>Avatar (optional)</Label>
      <div className="flex items-start gap-4">
        {/* Preview */}
        {previewUrl && !error ? (
          <div className="relative shrink-0">
            <img
              src={previewUrl}
              alt="Avatar preview"
              className="size-16 rounded-lg border object-cover"
              onError={(e) => {
                // If server returns 404, hide the broken img
                ;(e.target as HTMLImageElement).style.display = "none"
              }}
            />
            <button
              type="button"
              onClick={handleRemove}
              className="absolute -top-1.5 -right-1.5 rounded-full border bg-background p-0.5 text-muted-foreground hover:text-foreground"
            >
              <X className="size-3" />
            </button>
          </div>
        ) : null}

        {/* Drop zone / button */}
        <div
          onDragOver={(e) => {
            e.preventDefault()
            setDragOver(true)
          }}
          onDragLeave={() => setDragOver(false)}
          onDrop={handleDrop}
          className={`flex flex-1 cursor-pointer items-center justify-center rounded-lg border-2 border-dashed px-4 py-3 text-sm transition-colors ${
            dragOver
              ? "border-primary bg-primary/5"
              : "border-muted-foreground/25 hover:border-muted-foreground/50"
          }`}
          onClick={() => inputRef.current?.click()}
        >
          <Upload className="mr-2 size-4 text-muted-foreground" />
          <span className="text-muted-foreground">
            Drop image or click to browse
          </span>
        </div>

        <input
          ref={inputRef}
          type="file"
          accept={ACCEPT}
          className="hidden"
          onChange={handleInputChange}
        />
      </div>
      {error && <p className="text-xs text-red-500">{error}</p>}
      <p className="text-xs text-muted-foreground">
        PNG, JPEG, GIF, WebP or SVG. Max 2 MB.
      </p>
    </div>
  )
}

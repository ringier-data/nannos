import { client } from "./generated/client.gen"

client.interceptors.response.use((response) => {
  if (response.status === 401) {
    const redirectTo = encodeURIComponent(window.location.href)
    window.location.href = `/api/v2/auth/login?redirectTo=${redirectTo}`
  }
  return response
})

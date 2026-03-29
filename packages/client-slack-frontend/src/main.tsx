import { StrictMode } from "react"
import { createRoot } from "react-dom/client"
import { BrowserRouter, Routes, Route } from "react-router"

import "./index.css"
import "@/api/authInterceptors"
import App from "./App.tsx"
import { ThemeProvider } from "@/components/theme-provider.tsx"
import { AuthErrorPage } from "@/pages/AuthErrorPage.tsx"
import { InstallationsPage } from "@/pages/InstallationsPage.tsx"
import { NewInstallationPage } from "@/pages/NewInstallationPage.tsx"
import { EditInstallationPage } from "@/pages/EditInstallationPage.tsx"

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <ThemeProvider defaultTheme="light">
      <BrowserRouter>
        <Routes>
          <Route path="auth/error" element={<AuthErrorPage />} />
          <Route element={<App />}>
            <Route index element={<InstallationsPage />} />
            <Route path="installations/new" element={<NewInstallationPage />} />
            <Route
              path="installations/:appId"
              element={<EditInstallationPage />}
            />
          </Route>
        </Routes>
      </BrowserRouter>
    </ThemeProvider>
  </StrictMode>
)

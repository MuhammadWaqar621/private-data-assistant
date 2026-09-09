import { Route, Routes } from "react-router-dom";

import ProtectedRoute from "./components/ProtectedRoute";
import AppShellPage from "./pages/AppShellPage";
import ForgotPasswordPage from "./pages/ForgotPasswordPage";
import HomePage from "./pages/HomePage";
import LoginPage from "./pages/LoginPage";
import ResetPasswordPage from "./pages/ResetPasswordPage";
import SignupPage from "./pages/SignupPage";

export default function App() {
  return (
    <Routes>
      <Route path="/" element={<HomePage />} />
      <Route path="/login" element={<LoginPage />} />
      <Route path="/signup" element={<SignupPage />} />
      <Route path="/forgot-password" element={<ForgotPasswordPage />} />
      <Route path="/reset-password" element={<ResetPasswordPage />} />
      <Route
        path="/app"
        element={
          <ProtectedRoute>
            <AppShellPage />
          </ProtectedRoute>
        }
      />
    </Routes>
  );
}

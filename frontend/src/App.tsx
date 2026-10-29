import { AuthProvider, useAuth } from "./auth/AuthContext";
import { LoginScreen } from "./auth/LoginScreen";
import { KdsScreen } from "./kds/KdsScreen";

function Root() {
  const { state } = useAuth();

  switch (state.status) {
    case "restoring":
      return (
        <section className="state state--full" aria-busy="true">
          <div className="spinner" aria-hidden="true" />
          <p className="state__text">Restoring session…</p>
        </section>
      );
    case "anonymous":
      return <LoginScreen notice={state.notice} />;
    case "authenticated":
      return <KdsScreen token={state.token} user={state.user} />;
  }
}

export function App() {
  return (
    <AuthProvider>
      <Root />
    </AuthProvider>
  );
}

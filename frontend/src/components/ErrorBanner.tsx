export function ErrorBanner({ children }: { children: string }) {
  return (
    <div className="error-banner" role="alert">
      <span className="error-banner-icon" aria-hidden="true">!</span>
      <span>{children}</span>
    </div>
  );
}

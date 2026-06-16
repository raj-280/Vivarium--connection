import * as React from "react"
import { cn } from "@/lib/utils"

export interface BadgeProps extends React.HTMLAttributes<HTMLDivElement> {
  className?: string;
  children?: React.ReactNode;
  variant?: "default" | "secondary" | "destructive" | "outline" | "success" | "warning";
}

function Badge({ className, variant = "default", ...props }: BadgeProps) {
  return (
    <div
      className={cn(
        "inline-flex items-center rounded-full border px-2.5 py-0.5 text-xs font-semibold transition-colors focus:outline-none focus:ring-2 focus:ring-blue-500 focus:ring-offset-2",
        {
          "border-blue-200 bg-blue-100 text-blue-800": variant === "default",
          "border-gray-200 bg-gray-100 text-gray-800": variant === "secondary",
          "border-red-200 bg-red-100 text-red-800": variant === "destructive",
          "border-green-200 bg-green-100 text-green-800": variant === "success",
          "border-amber-200 bg-amber-100 text-amber-800": variant === "warning",
          "text-slate-800": variant === "outline",
        },
        className
      )}
      {...props}
    />
  )
}

export { Badge }

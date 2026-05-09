import { describe, expect, it, vi } from "vitest";
import { render, screen, fireEvent, act } from "@testing-library/react";
import RiskSlider from "@/components/RiskSlider";

describe("RiskSlider", () => {
  it("reflects the prop value in the label", () => {
    render(<RiskSlider value={3} onChange={() => {}} />);
    expect(screen.getByText(/Cautious/i)).toBeInTheDocument();
    expect(screen.getByText(/\[3\]/)).toBeInTheDocument();
  });

  it("renders correct label for each band", () => {
    const { rerender } = render(<RiskSlider value={1} onChange={() => {}} />);
    expect(screen.getByText(/Conservative/i)).toBeInTheDocument();
    rerender(<RiskSlider value={5} onChange={() => {}} />);
    expect(screen.getByText(/Balanced/i)).toBeInTheDocument();
    rerender(<RiskSlider value={9} onChange={() => {}} />);
    expect(screen.getByText(/Aggressive/i)).toBeInTheDocument();
  });

  it("debounces onChange by ~500ms", async () => {
    vi.useFakeTimers();
    const onChange = vi.fn();
    render(<RiskSlider value={5} onChange={onChange} />);
    const range = screen.getByRole("slider") as HTMLInputElement;

    fireEvent.change(range, { target: { value: "8" } });
    // Immediately: onChange should NOT have fired yet
    expect(onChange).not.toHaveBeenCalled();

    // Advance fake clock
    await act(async () => {
      vi.advanceTimersByTime(600);
    });
    expect(onChange).toHaveBeenCalledOnce();
    expect(onChange).toHaveBeenCalledWith(8);
    vi.useRealTimers();
  });

  it("collapses rapid changes into a single onChange", async () => {
    vi.useFakeTimers();
    const onChange = vi.fn();
    render(<RiskSlider value={5} onChange={onChange} />);
    const range = screen.getByRole("slider") as HTMLInputElement;

    fireEvent.change(range, { target: { value: "6" } });
    fireEvent.change(range, { target: { value: "7" } });
    fireEvent.change(range, { target: { value: "8" } });
    await act(async () => {
      vi.advanceTimersByTime(600);
    });
    expect(onChange).toHaveBeenCalledTimes(1);
    expect(onChange).toHaveBeenCalledWith(8);
    vi.useRealTimers();
  });

  it("respects disabled prop", () => {
    render(<RiskSlider value={5} onChange={() => {}} disabled />);
    const range = screen.getByRole("slider") as HTMLInputElement;
    expect(range.disabled).toBe(true);
  });
});

using System;
using System.Threading;
using System.Runtime.InteropServices;
using WindowsInput.Native;

namespace WindowsInput
{
  /// <summary>
  /// 在 <see cref="InputSimulator"/> 之上的拟人化封装：
  /// 贝塞尔曲线 + 噪声抖动的鼠标轨迹、随机反应/按压时长的点击、随机间隔的数字键入。
  /// 仅改变“动作的路径与时序”，不改变业务调用顺序。
  /// 坐标沿用底层库约定：绝对坐标为 0~65535 的归一化坐标，另有像素重载方便直接使用截图坐标。
  /// </summary>
  public class HumanInputSimulator
  {
    /// <summary>
    /// 底层 InputSimulator，需要本封装未覆盖的原语时可直接使用。
    /// </summary>
    private readonly InputSimulator _simulator;

    /// <summary>
    /// 随机数与手势串行化所用的锁，避免多线程手势互相穿插。
    /// </summary>
    private readonly object _syncRoot = new object();

    private readonly Random _random = new Random();

    /// <summary>
    /// 使用默认的 <see cref="InputSimulator"/> 与拟人化默认参数初始化。
    /// </summary>
    public HumanInputSimulator()
    {
      _simulator = new InputSimulator();
      ApplyDefaults();
    }

    /// <summary>
    /// 使用指定的 <see cref="InputSimulator"/> 初始化（便于替换消息分发器等）。
    /// </summary>
    /// <param name="simulator">被包装的 InputSimulator 实例。</param>
    public HumanInputSimulator(InputSimulator simulator)
    {
      if (simulator == null) throw new ArgumentNullException("simulator");
      _simulator = simulator;
      ApplyDefaults();
    }

    /// <summary>
    /// 设置拟人化默认参数。
    /// </summary>
    private void ApplyDefaults()
    {
      MoveStepMinMs = 4;
      MoveStepMaxMs = 12;
      JitterNormalized = 180;
      OvershootChance = 0.15;
      ClickBeforeMeanMs = 140;
      ClickBeforeStdMs = 55;
      ClickHoldMeanMs = 70;
      ClickHoldStdMs = 28;
      KeyGapMeanMs = 145;
      KeyGapStdMs = 60;
    }

    /// <summary>获取底层实例，用于本封装未提供的操作。</summary>
    public IInputSimulator Base
    {
      get { return _simulator; }
    }

    /// <summary>轨迹每步移动之间的最小停顿(毫秒)。</summary>
    public int MoveStepMinMs { get; set; }

    /// <summary>轨迹每步移动之间的最大停顿(毫秒)。</summary>
    public int MoveStepMaxMs { get; set; }

    /// <summary>轨迹抖动强度，单位为 0~65535 归一化坐标，1080p 下约 180 ≈ 3 像素。</summary>
    public double JitterNormalized { get; set; }

    /// <summary>移动越过目标再回拉修正的概率，取值 0~1。</summary>
    public double OvershootChance { get; set; }

    /// <summary>点击前“反应时间”的均值(毫秒)。</summary>
    public int ClickBeforeMeanMs { get; set; }

    /// <summary>点击前“反应时间”的标准差(毫秒)。</summary>
    public int ClickBeforeStdMs { get; set; }

    /// <summary>鼠标按下到抬起之间按压时长的均值(毫秒)。</summary>
    public int ClickHoldMeanMs { get; set; }

    /// <summary>鼠标按压时长的标准差(毫秒)。</summary>
    public int ClickHoldStdMs { get; set; }

    /// <summary>数字键之间间隔的均值(毫秒)。</summary>
    public int KeyGapMeanMs { get; set; }

    /// <summary>数字键之间间隔的标准差(毫秒)。</summary>
    public int KeyGapStdMs { get; set; }

    /// <summary>
    /// 用拟人化默认参数创建一个实例。
    /// </summary>
    /// <returns>带默认参数的 <see cref="HumanInputSimulator"/>。</returns>
    public static HumanInputSimulator CreateWithDefaults()
    {
      return new HumanInputSimulator();
    }

    /// <summary>
    /// 拟人化移动到归一化坐标(0~65535)。
    /// </summary>
    /// <param name="normalizedX">目标 X，0~65535。</param>
    /// <param name="normalizedY">目标 Y，0~65535。</param>
    public void MoveTo(double normalizedX, double normalizedY)
    {
      lock (_syncRoot)
      {
        MoveGesture(normalizedX, normalizedY);
      }
    }

    /// <summary>
    /// 拟人化移动到主屏幕上的像素坐标（会自动换算为归一化坐标）。
    /// </summary>
    /// <param name="pixelX">目标像素 X。</param>
    /// <param name="pixelY">目标像素 Y。</param>
    public void MoveToPixel(int pixelX, int pixelY)
    {
      double nx = pixelX * 65535.0 / GetSystemMetrics(SM_CXSCREEN);
      double ny = pixelY * 65535.0 / GetSystemMetrics(SM_CYSCREEN);
      MoveTo(nx, ny);
    }

    /// <summary>
    /// 在当前位置执行一次拟人化左键点击（随机反应延迟 + 随机按压时长）。
    /// </summary>
    public void LeftClick()
    {
      lock (_syncRoot)
      {
        ClickGesture();
      }
    }

    /// <summary>
    /// 拟人化移动到归一化坐标后左键点击。
    /// </summary>
    /// <param name="normalizedX">目标 X，0~65535。</param>
    /// <param name="normalizedY">目标 Y，0~65535。</param>
    public void ClickAt(double normalizedX, double normalizedY)
    {
      lock (_syncRoot)
      {
        MoveGesture(normalizedX, normalizedY);
        ClickGesture();
      }
    }

    /// <summary>
    /// 拟人化移动到像素坐标后左键点击。
    /// </summary>
    /// <param name="pixelX">目标像素 X。</param>
    /// <param name="pixelY">目标像素 Y。</param>
    public void ClickAtPixel(int pixelX, int pixelY)
    {
      double nx = pixelX * 65535.0 / GetSystemMetrics(SM_CXSCREEN);
      double ny = pixelY * 65535.0 / GetSystemMetrics(SM_CYSCREEN);
      ClickAt(nx, ny);
    }

    /// <summary>
    /// 拟人化键入一串数字（仅 0~9 会被敲击，其余字符跳过），键间加入随机间隔。
    /// </summary>
    /// <param name="digits">包含数字的字符串，例如数独答案 "53007..."。</param>
    public void TypeDigits(string digits)
    {
      if (digits == null) throw new ArgumentNullException("digits");
      lock (_syncRoot)
      {
        for (int i = 0; i < digits.Length; i++)
        {
          char c = digits[i];
          if (c >= '0' && c <= '9')
          {
            if (i > 0)
            {
              HumanPause(KeyGapMeanMs, KeyGapStdMs, 55, 420);
            }
            _simulator.Keyboard.KeyPress(DigitToKey(c));
          }
        }
      }
    }

    /// <summary>
    /// 数独填表专用：移动到某格像素坐标 -> 点击 -> 输入一个数字。
    /// </summary>
    /// <param name="pixelX">格子中心像素 X。</param>
    /// <param name="pixelY">格子中心像素 Y。</param>
    /// <param name="digit">要填入的数字字符（0~9，其余字符将被跳过且不点击）。</param>
    public void FillCell(int pixelX, int pixelY, char digit)
    {
      if (digit < '0' || digit > '9')
      {
        return;
      }
      lock (_syncRoot)
      {
        double nx = pixelX * 65535.0 / GetSystemMetrics(SM_CXSCREEN);
        double ny = pixelY * 65535.0 / GetSystemMetrics(SM_CYSCREEN);
        MoveGesture(nx, ny);
        ClickGesture();
        HumanPause(KeyGapMeanMs / 2, KeyGapStdMs / 2, 40, 300);
        _simulator.Keyboard.KeyPress(DigitToKey(digit));
      }
    }

    /// <summary>
    /// 核心移动手势：二次贝塞尔轨迹 + 高斯抖动 + 可选过冲回拉。
    /// </summary>
    private void MoveGesture(double targetX, double targetY)
    {
      PointApi current;
      GetCursorPos(out current);
      int screenWidth = GetSystemMetrics(SM_CXSCREEN);
      int screenHeight = GetSystemMetrics(SM_CYSCREEN);

      double startX = current.X * 65535.0 / screenWidth;
      double startY = current.Y * 65535.0 / screenHeight;
      double dx = targetX - startX;
      double dy = targetY - startY;
      double distance = Math.Sqrt(dx * dx + dy * dy);

      if (distance < 1.0)
      {
        _simulator.Mouse.MoveMouseTo(targetX, targetY);
        return;
      }

      // 是否越过目标：把贝塞尔终点放到目标前方 1.5%~4% 距离处，之后再回拉修正。
      double aimX = targetX;
      double aimY = targetY;
      bool overshoot = NextRandomDouble() < OvershootChance;
      if (overshoot)
      {
        double overshootRatio = 0.015 + NextRandomDouble() * 0.025;
        aimX = targetX + dx / distance * distance * overshootRatio;
        aimY = targetY + dy / distance * distance * overshootRatio;
      }

      // 控制点 = 起点终点中点 + 垂直方向上 ±10% 距离的随机弧度，形成弧形轨迹。
      double midX = (startX + aimX) / 2.0;
      double midY = (startY + aimY) / 2.0;
      double perpendicularX = -dy / distance;
      double perpendicularY = dx / distance;
      double arc = (NextRandomDouble() * 2.0 - 1.0) * 0.10 * distance;
      double controlX = midX + perpendicularX * arc;
      double controlY = midY + perpendicularY * arc;

      double stepLength = 900.0 + NextRandomDouble() * 700.0;
      int steps = (int)Math.Round(distance / stepLength);
      steps = Math.Max(14, Math.Min(55, steps));

      for (int i = 1; i <= steps; i++)
      {
        double t = EaseInOutQuad((double)i / steps);
        double oneMinusT = 1.0 - t;

        double bx = oneMinusT * oneMinusT * startX
                  + 2.0 * oneMinusT * t * controlX
                  + t * t * aimX;
        double by = oneMinusT * oneMinusT * startY
                  + 2.0 * oneMinusT * t * controlY
                  + t * t * aimY;

        // 越接近目标抖动越小，避免落点飘走。
        double jitterScale = JitterNormalized * (1.0 - 0.75 * t);
        bx += NextGaussian() * jitterScale;
        by += NextGaussian() * jitterScale;

        _simulator.Mouse.MoveMouseTo(bx, by);
        StepPause();
      }

      if (overshoot)
      {
        int correctionSteps = 3 + NextRandom(0, 4);
        for (int i = 1; i <= correctionSteps; i++)
        {
          double t = EaseInOutQuad((double)i / correctionSteps);
          double cx2 = aimX + (targetX - aimX) * t + NextGaussian() * JitterNormalized * 0.25;
          double cy2 = aimY + (targetY - aimY) * t + NextGaussian() * JitterNormalized * 0.25;
          _simulator.Mouse.MoveMouseTo(cx2, cy2);
          Thread.Sleep(NextRandom(8, 21));
        }
      }

      // 精确落到目标点，保证点击坐标准确。
      _simulator.Mouse.MoveMouseTo(targetX, targetY);
    }

    /// <summary>
    /// 核心点击手势：移动到位后的反应停顿、按下、随机保持、抬起。
    /// </summary>
    private void ClickGesture()
    {
      HumanPause(ClickBeforeMeanMs, ClickBeforeStdMs, 50, 420);
      _simulator.Mouse.LeftButtonDown();
      HumanPause(ClickHoldMeanMs, ClickHoldStdMs, 28, 200);
      _simulator.Mouse.LeftButtonUp();
      Thread.Sleep(NextRandom(10, 60));
    }

    /// <summary>
    /// 二次缓动：起步慢、中段快、末段慢。
    /// </summary>
    private static double EaseInOutQuad(double t)
    {
      if (t < 0.5)
      {
        return 2.0 * t * t;
      }
      return 1.0 - Math.Pow(-2.0 * t + 2.0, 2.0) / 2.0;
    }

    /// <summary>
    /// 轨迹步间停顿：基础随机间隔 + 4% 概率出现一次“犹豫”。
    /// </summary>
    private void StepPause()
    {
      int ms = NextRandom(MoveStepMinMs, MoveStepMaxMs + 1);
      if (NextRandomDouble() < 0.04)
      {
        ms += NextRandom(30, 111);
      }
      Thread.Sleep(ms);
    }

    /// <summary>
    /// 高斯分布的拟人停顿，并裁剪到 [minMs, maxMs]。
    /// </summary>
    private void HumanPause(int meanMs, int stdMs, int minMs, int maxMs)
    {
      int ms = (int)Math.Round(meanMs + NextGaussian() * stdMs);
      ms = Math.Max(minMs, Math.Min(maxMs, ms));
      Thread.Sleep(ms);
    }

    /// <summary>
    /// 数字字符到虚拟键码的映射。
    /// </summary>
    private static VirtualKeyCode DigitToKey(char digit)
    {
      switch (digit)
      {
        case '0': return VirtualKeyCode.VK_0;
        case '1': return VirtualKeyCode.VK_1;
        case '2': return VirtualKeyCode.VK_2;
        case '3': return VirtualKeyCode.VK_3;
        case '4': return VirtualKeyCode.VK_4;
        case '5': return VirtualKeyCode.VK_5;
        case '6': return VirtualKeyCode.VK_6;
        case '7': return VirtualKeyCode.VK_7;
        case '8': return VirtualKeyCode.VK_8;
        default: return VirtualKeyCode.VK_9;
      }
    }

    /// <summary>线程安全的均匀随机整数。</summary>
    private int NextRandom(int minValue, int maxValue)
    {
      lock (_syncRoot)
      {
        return _random.Next(minValue, maxValue);
      }
    }

    /// <summary>线程安全的 [0,1) 随机数。</summary>
    private double NextRandomDouble()
    {
      lock (_syncRoot)
      {
        return _random.NextDouble();
      }
    }

    /// <summary>
    /// Box-Muller 变换生成标准正态分布随机数（均值 0，标准差 1）。
    /// </summary>
    private double NextGaussian()
    {
      lock (_syncRoot)
      {
        double u1 = 1.0 - _random.NextDouble();
        double u2 = 1.0 - _random.NextDouble();
        return Math.Sqrt(-2.0 * Math.Log(u1)) * Math.Sin(2.0 * Math.PI * u2);
      }
    }

    private const int SM_CXSCREEN = 0;
    private const int SM_CYSCREEN = 1;

    [DllImport("user32.dll")]
    private static extern int GetSystemMetrics(int nIndex);

    [DllImport("user32.dll")]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static extern bool GetCursorPos(out PointApi lpPoint);

    /// <summary>Win32 POINT 结构。</summary>
    private struct PointApi
    {
      public int X;
      public int Y;
    }
  }
}

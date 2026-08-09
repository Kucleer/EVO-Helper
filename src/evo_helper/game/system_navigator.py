"""恒星系导航栏：把坐标输进去，让行星详情面板显示出来。

实测出来的两条行为，改这里之前先读一遍：

- **点任一输入框会弹出覆盖整条导航栏的编辑浮层**，所以一次只能设一个字段：
  点字段 → 输入 → 点 OK → 再设下一个。一口气点两个字段，第二个数字会进第一个字段。
- **`«` / `»` 是切换恒星系，不是翻页**，而且会关掉详情面板回到恒星系视图。
  扫描不用它：恒星系视图是一张可平移的散点图，行星没有位号标签，
  看不出「这一屏是不是 20 个位都看到了」——覆盖度无法自证，会留下静默缺口。

导航是只读操作，但仍然走 `HumanInput`：固定节奏的点击是最明显的自动化特征。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

from evo_helper.domain.models import Coordinate

#: 导航栏三个输入框与 OK（截图 client 空间，标定视口 1920×879 + 38px 标题栏）。
GALAXY_FIELD = (795, 71)
SYSTEM_FIELD = (959, 71)
POSITION_FIELD = (1122, 71)
OK_BUTTON = (1029, 132)

#: 切视图：点底部左侧的星球按钮弹出子菜单，子菜单里**只列出你现在不在的那些视图**。
#: 游戏会自己回到行星视图（会话刷新时实测发生过），所以这条路径是必需的，不是可选的。
VIEW_MENU_BUTTON = (749, 856)

#: 子菜单第二项（带环行星图标）。**同一个像素，含义随当前视图而变**：
#: 在行星地表上点它 → 回恒星系视图；在恒星系视图上点它 → 去行星地表。
#: （第一项是银河系视图，图标是螺旋。）
#:
#: 所以这个常量有两个名字，指向同一处：按当前在哪来读它，别按名字想当然。
#: 用户 2026-08-09 指出：回星球地表要走这个菜单，**不要**走底部导航的「行星」——
#: 那个开出来的是行星列表浮层，一排八个图标全是真实操作（运输/部署/传送/转移…）。
SYSTEM_VIEW_BUTTON = (737, 748)
PLANET_VIEW_BUTTON = SYSTEM_VIEW_BUTTON

#: 导航栏三个字段的**标签**所在的横条。行星视图上这里是空的，可以用来判断在不在恒星系视图。
NAV_LABEL_ROI = (740, 88, 1190, 115)
NAV_LABELS = ("银河系", "恒星系", "行星")

#: 点开字段后等编辑浮层弹出。
FIELD_OPEN_WAIT_S = 0.7

#: 点 OK 后等画面切换。切屏时间不稳定，这是下限而不是精确值。
APPLY_WAIT_S = 2.6

#: 切视图后等画面切换。
VIEW_SWITCH_WAIT_S = 2.0


def on_system_view(text: str) -> bool:
    """导航栏标签读到两个以上就算在恒星系视图。

    要两个而不是一个：`行星` 在底部导航条上也有，单个标签命中不足以证明在哪一屏。
    """
    return sum(label in text for label in NAV_LABELS) >= 2


class ScanDriver(Protocol):
    """扫描需要的最小操作面。真实实现驱动鼠标，测试里换成假的。"""

    def click(self, x: int, y: int, *, label: str = ...) -> None: ...

    def type_number(self, value: int) -> None: ...

    def capture(self) -> Any: ...

    def wait(self, seconds: float) -> None: ...


@dataclass
class SystemNavigator:
    """按坐标导航，并记住当前停在哪，好省掉不必要的字段输入。

    银河系和恒星系只在变化时才重设——一次字段输入是三个动作约 3 秒，
    同一恒星系内连扫 16 个位时省下的时间占了大头。
    """

    driver: ScanDriver
    #: 当前导航栏里的坐标。None 表示不确定，下一次会把三个字段都设一遍。
    current: Coordinate | None = None

    def goto(self, coordinate: Coordinate) -> None:
        at = self.current
        # 位置永远重设：它是本次要读的那个字段，不能靠推断。
        if at is None or at.galaxy != coordinate.galaxy:
            self._set(GALAXY_FIELD, coordinate.galaxy, "银河系")
        if at is None or at.system != coordinate.system:
            self._set(SYSTEM_FIELD, coordinate.system, "恒星系")
        self._set(POSITION_FIELD, coordinate.position, "行星")
        self.current = coordinate

    def invalidate(self) -> None:
        """画面被别的东西改过（重连、弹窗）之后调用，下一次重设全部字段。"""
        self.current = None

    def ensure_system_view(self, read_nav_labels: Callable[[], str], *, attempts: int = 3) -> bool:
        """确保停在恒星系视图；不在就走「星球按钮 → 恒星系」把它切回来。

        判据是导航栏标签而不是画面差异——切视图的动画期间画面会变，但那不等于切到了。
        试满 `attempts` 次仍读不到标签就返回 False，由调用方停止：接着往
        `(795, 71)` 之类的固定坐标点下去，就是在认不出的画面上乱点。
        """
        for attempt in range(attempts):
            if on_system_view(read_nav_labels()):
                return True
            self.driver.click(*VIEW_MENU_BUTTON, label="视图菜单")
            self.driver.wait(1.0)
            self.driver.click(*SYSTEM_VIEW_BUTTON, label="恒星系视图")
            self.driver.wait(VIEW_SWITCH_WAIT_S * (attempt + 1))
            # 视图换过之后导航栏里是什么已经不可知了。
            self.invalidate()
        return on_system_view(read_nav_labels())

    def _set(self, field: tuple[int, int], value: int, label: str) -> None:
        self.driver.click(*field, label=label)
        self.driver.wait(FIELD_OPEN_WAIT_S)
        self.driver.type_number(value)
        self.driver.click(*OK_BUTTON, label="OK")
        self.driver.wait(APPLY_WAIT_S)


def crop_reader(image: Any, ocr: Callable[..., str]) -> Callable[..., str]:
    """把一张截图包成 `scan_reading.read_panel` 要的取字函数。"""

    def read(box: tuple[int, int, int, int], **recipe: Any) -> str:
        # 配方原样透传：坐标行会带上 resample，名字行不带。
        return str(ocr(image.crop(box), **recipe))

    return read

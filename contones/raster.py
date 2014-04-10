"""Raster data handling"""
import numpy as np
from PIL import Image, ImageDraw
from osgeo import gdal, gdalconst

import contones.gio
from contones.geometry import Envelope
from contones.srs import SpatialReference

def geom_to_array(geom, matrix_size, affine):
    """Converts an OGR polygon to a 2D NumPy array.

    Arguments:
    geom -- OGR Polygon or MultiPolygon
    matrix_size -- array size in pixels as a tuple of (width, height)
    affine -- AffineTransform
    """
    img = Image.new('L', matrix_size, 1)
    draw = ImageDraw.Draw(img)
    if not geom.GetGeometryName().startswith('MULTI'):
        geom = [geom]
    for g in geom:
        if g.GetCoordinateDimension() > 2:
            g.FlattenTo2D()
        boundary = g.Boundary()
        coords = boundary.GetPoints() if boundary else g.GetPoints()
        draw.polygon(affine.transform(coords), 0)
    return np.asarray(img)

def count_unique(arr):
    """Returns a two-tuple of pixel count and bin value for every unique pixel
    value in the array.

    Arguments:
    arr -- numpy ndarray
    """
    return [a.tolist() for a in np.histogram(arr, np.unique(arr))]


class AffineTransform(object):
    """Affine transformation between projected and pixel coordinate spaces."""

    def __init__(self, geotrans_tuple):
        """
        Arguments:
        geotrans_tuple -- geotransformation as a five element tuple like
            (-124.625, 0.125, 0.0, 44.0, 0.0, -0.125,).
        """
        # Origin coordinate in projected space.
        self.origin = geotrans_tuple[0], geotrans_tuple[3]
        self.scale_x = geotrans_tuple[1]
        self.scale_y = geotrans_tuple[5]

    def __repr__(self):
        return str(self.tuple)

    def __eq__(self, another):
        return self.tuple == getattr(another, 'tuple', None)

    def __ne__(self, another):
        return not self.__eq__(another)

    #def __getitem__(self, idx):
        #return self.tuple[idx]

    def transform_to_projected(self, coords):
        """Convert image pixel/line coordinates to georeferenced x/y, return a
        generator of two-tuples.

        Arguments:
        coords -- input coordinates as iterable containing two-tuples/lists
        such as ((0, 0), (10, 10))
        """
        geotransform = self.tuple
        for x, y in coords:
            geo_x = geotransform[0] + geotransform[1] * x + geotransform[2] * y
            geo_y = geotransform[3] + geotransform[4] * x + geotransform[5] * y
            # Move the coordinate to the center of the pixel.
            geo_x += geotransform[1] / 2.0
            geo_y += geotransform[5] / 2.0
            yield geo_x, geo_y

    def transform(self, coords):
        """Transform from projection coordinates (Xp,Yp) space to pixel/line
        (P,L) raster space, based on the provided geotransformation.

        Arguments:
        coords -- input coordinates as iterable containing two-tuples/lists
        such as ((-120, 38), (-121, 39))
        """
        # Use local vars for better performance here.
        origin = self.origin
        sx = self.scale_x
        sy = self.scale_y
        return [(int((x - origin[0]) / sx), int((y - origin[1]) / sy))
                for x, y in coords]

    @property
    def tuple(self):
        # Assumes north up images.
        return (self.origin[0], self.scale_x, 0.0, self.origin[1], 0.0,
                self.scale_y)


class Raster(object):
    """Wrap a GDAL Dataset with additional behavior."""

    def __init__(self, dataset, mode=gdalconst.GA_ReadOnly):
        """Initialize a Raster data set from a path or file

        Arguments:
        dataset -- path as str or file object
        Keyword args:
        mode -- gdal constant representing access mode
        """
        # Get the name if we have a file object.
        dataset = getattr(dataset, 'name', dataset)
        if not isinstance(dataset, gdal.Dataset):
            dataset = gdal.Open(dataset, mode)
        if dataset is None:
            raise IOError('Could not open %s' % dataset)
        self.ds = dataset
        self.name = self.ds.GetDescription()
        self.affine = AffineTransform(self.GetGeoTransform())
        self.sref = SpatialReference(dataset.GetProjection())
        self._nodata = None
        self._envelope = None
        self._driver = None
        # Closes the GDALDataset
        dataset = None

    def __getattr__(self, attr):
        """Delegate calls to the GDALDataset."""
        return getattr(self.ds, attr)

    def __getitem__(self, i):
        """Returns a single Band instance.

        This is a one-based index which matches the GDAL approach of handling
        multiband images.
        """
        band = self.GetRasterBand(i)
        if not band:
            raise IndexError('No band for {}'.format(i))
        return band

    #TODO: handle subdataset iteration
    def __iter__(self):
        # Bands are not zero based
        for i in range(1, self.RasterCount + 1):
            yield self[i]

    def __del__(self):
        self.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()
        return True

    def __len__(self):
        return self.RasterCount

    def __eq__(self, another):
        if type(another) is type(self):
            return self.__dict__ == another.__dict__
        return False

    def __ne__(self, another):
        return not self.__eq__(another)

    def __repr__(self):
        return '{}({})'.format(self.__class__.__name__,
                               self.ds.GetDescription())

    def array(self, envelope=()):
        """Returns an NDArray, optionally subset by spatial envelope.

        Keyword args:
        envelope -- coordinate extent tuple or Envelope
        """
        args = ()
        if envelope:
            args = self.get_offset(envelope)
        return self.ds.ReadAsArray(*args)

    def close(self):
        """Close the GDAL dataset."""
        # De-ref the GDAL Dataset to completely close it.
        self.ds = None

    def crop(self, bbox):
        """Returns a new raster instance cropped to a bounding box.

        Arguments:
        bbox -- bounding box as an OGR Polygon
        """
        return self._mask(bbox)

    @property
    def envelope(self):
        """Returns the minimum bounding rectangle as a tuple of min X, min Y,
        max X, max Y.
        """
        if self._envelope is None:
            origin = self.affine.origin
            ur_x = origin[0] + self.RasterXSize * self.affine.scale_x
            ll_y = origin[1] + self.RasterYSize * self.affine.scale_y
            self._envelope = Envelope(origin[0], ll_y, ur_x, origin[1])
        return self._envelope

    def get_offset(self, envelope):
        """Returns a 4-tuple pixel window (x_offset, y_offset, x_size, y_size).

        Arguments:
        envelope -- coordinate extent tuple or Envelope
        """
        if isinstance(envelope, tuple):
            envelope = Envelope(*envelope)
        if not (self.envelope.contains(envelope) or
                self.envelope.intersects(envelope)):
            raise ValueError('Envelope does not intersect with this extent.')
        ul_px, lr_px = self.affine.transform((envelope.ul, envelope.lr))
        nx = min(lr_px[0] - ul_px[0], self.RasterXSize - ul_px[0])
        ny = min(lr_px[1] - ul_px[1], self.RasterYSize - ul_px[1])
        return ul_px + (nx, ny)

    @property
    def driver(self):
        """Returns the underlying ImageDriver instance."""
        if self._driver is None:
            self._driver = contones.gio.ImageDriver(self.ds.GetDriver())
        return self._driver

    def new(self, pixeldata=None, size=(), affine=None):
        """Derive new Raster instances.

        Keyword args:
        pixeldata -- bytestring containing pixel data
        size -- tuple of image size (width, height)
        affine -- affine transformation tuple
        """
        size = size or self.shape
        band = self.GetRasterBand(1)
        imgio = contones.gio.ImageIO(driver=self.driver.format)
        rcopy = imgio.create(size, band.DataType)
        imgio.close()
        rcopy.SetProjection(self.GetProjection())
        rcopy.SetGeoTransform(affine or self.GetGeoTransform())
        colors = band.GetColorTable()
        for outband in rcopy:
            outband.SetNoDataValue(self.nodata)
            if colors:
                outband.SetColorTable(colors)
        if pixeldata:
            args = (0, 0) + size + (pixeldata,)
            rcopy.WriteRaster(*args)
        return rcopy

    def _mask(self, geom):
        geom = self._transform_maskgeom(geom)
        env = Envelope.from_geom(geom)
        readargs = self.get_offset(env)
        dims = readargs[2:4]
        affine = AffineTransform(self.GetGeoTransform())
        # Update origin coordinate for the new affine transformation.
        affine.origin = env.ul
        # Without a simple envelope, this becomes a masking operation rather
        # than a crop.
        if not geom.Equals(env.to_geom()):
            arr = self.ds.ReadAsArray(*readargs)
            mask_arr = geom_to_array(geom, dims, affine)
            m = np.ma.masked_array(arr, mask=mask_arr)
            #m.set_fill_value(self.nodata)
            if self.nodata is not None:
                m = np.ma.masked_values(m, self.nodata)
            pixbuf = str(np.getbuffer(m.filled()))
        else:
            pixbuf = self.ds.ReadRaster(*readargs)
        clone = self.new(pixbuf, dims, affine.tuple)
        return clone

    def mask(self, geom):
        """Returns a new raster instance masked to a particular geometry.

        Arguments:
        geom -- OGR Polygon or MultiPolygon
        """
        return self._mask(geom)

    def masked_array(self, envelope=()):
        """Returns a MaskedArray using nodata values.

        Keyword args:
        envelope -- coordinate extent tuple or Envelope
        """
        arr = self.array(envelope)
        if self.nodata is None:
            return np.ma.masked_array(arr)
        return np.ma.masked_values(arr, self.nodata)

    @property
    def nodata(self):
        """Returns read only property for band nodata value, assuming single
        band rasters for now.
        """
        if self._nodata is None:
            self._nodata = self[1].GetNoDataValue()
        return self._nodata

    def ReadRaster(self, *args, **kwargs):
        """Returns a string of raster data for partial or full extent.

        Overrides GDALDataset.ReadRaster() with the full raster size by
        default.
        """
        if len(args) < 4:
            args = (0, 0, self.RasterXSize, self.RasterYSize)
        return self.ds.ReadRaster(*args, **kwargs)

    def resample(self, size, interpolation=gdalconst.GRA_NearestNeighbour):
        """Returns a new instance resampled to provided size.

        Arguments:
        size -- tuple of x,y image dimensions
        """
        # Find the scaling factor for pixel size.
        factors = (size[0] / float(self.RasterXSize),
                   size[1] / float(self.RasterYSize))
        affine = AffineTransform(self.GetGeoTransform())
        affine.scale_x *= factors[0]
        affine.scale_y *= factors[1]
        dest = self.new(size=size, affine=affine.tuple)
        # Uses self and dest projection when set to None
        gdal.ReprojectImage(self.ds, dest.ds, None, None, interpolation)
        return dest

    def save(self, to, driver=None):
        """Save this instance to the path and format provided.

        Arguments:
        to -- output path as str or ImageIO instance
        Keyword args:
        driver -- GDAL driver name as string
        """
        if not isinstance(to, contones.gio.ImageIO):
            to = contones.gio.ImageIO(to)
        driver = contones.gio.ImageDriver(driver) if driver else to.driver
        r = driver.copy(self, to)
        r.close()

    def SetProjection(self, to_sref):
        if not hasattr(to_sref, 'ExportToWkt'):
            to_sref = SpatialReference(to_sref)
        self.sref = to_sref
        self.ds.SetProjection(to_sref.ExportToWkt())

    def SetGeoTransform(self, geotrans_tuple):
        """Sets the affine transformation."""
        self.affine = AffineTransform(geotrans_tuple)
        self.ds.SetGeoTransform(geotrans_tuple)

    @property
    def shape(self):
        """Returns a tuple of row, column, (band count if multidimensional)."""
        shp = (self.RasterYSize, self.RasterXSize, self.RasterCount)
        return shp[:2] if shp[2] <= 1 else shp

    def _transform_maskgeom(self, geom):
        if isinstance(geom, Envelope):
            geom = geom.to_geom()
        geom_sref = geom.GetSpatialReference()
        if geom_sref is None:
            raise Exception('Cannot transform from unknown spatial reference')
        # Reproject geom if necessary
        if not geom_sref.IsSame(self.sref):
            geom = geom.Clone()
            geom.TransformTo(self.sref)
        return geom

    def warp(self, to_sref, interpolation=gdalconst.GRA_NearestNeighbour):
        """Returns a new reprojected instance.

        Arguments:
        to_sref -- spatial reference as a proj4 or wkt string, or a
        SpatialReference
        """
        if not hasattr(to_sref, 'ExportToWkt'):
            to_sref = SpatialReference(to_sref)
        dest_wkt = to_sref.ExportToWkt()
        dtype = self[1].DataType
        err_thresh = 0.125
        # Call AutoCreateWarpedVRT() to fetch default values for target raster
        # dimensions and geotransform
        # src_wkt : left to default value --> will use the one from source
        vrt = gdal.AutoCreateWarpedVRT(self.ds, None, dest_wkt, interpolation,
                                       err_thresh)
        dst_xsize = vrt.RasterXSize
        dst_ysize = vrt.RasterYSize
        dst_gt = vrt.GetGeoTransform()
        vrt = None
        # FIXME: Should not set proj in new()?
        imgio = contones.gio.ImageIO(driver=self.driver.format)
        dest = imgio.create((dst_xsize, dst_ysize, self.RasterCount), dtype)
        imgio.close()
        dest.SetGeoTransform(dst_gt)
        dest.SetProjection(to_sref)
        for band in dest:
            band.SetNoDataValue(self.nodata)
            band = None
        # Uses self and dest projection when set to None
        gdal.ReprojectImage(self.ds, dest.ds, None, None, interpolation)
        return dest


open = Raster
